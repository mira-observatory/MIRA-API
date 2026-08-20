"""Corre las preguntas de referencia contra el pipeline real y evalua los
invariantes de cada una.

Cada caso es una llamada real a Claude con costo real, asi que esto **no**
corre con las pruebas normales. Se invoca a mano:

    python -m mira_api.evals.runner

Los renglones que deja en analytics.query_log llevan subject_key EVAL-RUN,
para poder distinguirlos de preguntas de personas al mirar el registro.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from mira_api.api.schemas import ConversationTurn, QueryRequest, QueryResponse
from mira_api.config import Settings, get_settings
from mira_api.db.executor import ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.db.pool import build_log_pool, build_read_pool
from mira_api.evals.cases import CASES, Case
from mira_api.llm.client import ClaudeClient
from mira_api.nlq.pipeline import run_query, wait_for_audit_tasks
from mira_api.nlq.semantic_dictionary import load_semantic_dictionary
from mira_api.nlq.sql_generation import build_system_blocks
from mira_api.nlq.validator import SqlRejected, validate

#: Marca los renglones de auditoria que dejo esta suite.
EVAL_SUBJECT = "EVAL-RUN"


@dataclass(frozen=True)
class CaseResult:
    case: Case
    ok: bool
    failures: list[str]
    outcome: str
    row_count: int
    sql: str | None


def _relations_of(sql: str, countries: list[str], max_rows: int) -> frozenset[str]:
    """Las vistas que toca un SQL, segun el propio validador. Se re-valida en
    vez de buscar substrings para que 'v_process' no coincida dentro de
    'v_process_buyers'."""
    try:
        return validate(sql, max_rows=max_rows, countries=countries).relations
    except SqlRejected:
        return frozenset()


def _countries_in(sql: str) -> frozenset[str]:
    """Codigos ISO citados como literal en el SQL. Basta para comprobar por
    que pais se filtro: el validador ya garantizo que hay un predicado sobre
    country_code y que sus valores estan dentro de lo pedido."""
    found = set()
    upper = sql.upper()
    for code in ("CR", "GT", "HN", "NI", "SV", "PA"):
        if f"'{code}'" in upper:
            found.add(code)
    return frozenset(found)


def evaluate(case: Case, response: QueryResponse, *, max_rows: int) -> CaseResult:
    """Compara la respuesta contra los invariantes del caso. Puro: no toca red
    ni base, para poder probarlo sin gastar."""
    failures: list[str] = []

    if response.outcome not in case.allowed_outcomes:
        esperados = ", ".join(sorted(o.value for o in case.allowed_outcomes))
        failures.append(f"outcome {response.outcome.value}, se esperaba uno de: {esperados}")

    sql = response.sql_executed
    if case.expect_sql and sql is None:
        failures.append("no genero SQL y se esperaba que lo hiciera")
    if not case.expect_sql and sql is not None and case.expect_relations:
        # Solo es contradiccion si ademas se exigian vistas concretas.
        failures.append("genero SQL y no se esperaba")

    if sql is not None:
        relations = _relations_of(sql, case.countries, max_rows)
        faltantes = case.expect_relations - relations
        if faltantes:
            failures.append(f"el SQL no toca {', '.join(sorted(faltantes))}")

        if case.expect_countries:
            filtrados = _countries_in(sql)
            sobran = filtrados - case.expect_countries
            faltan = case.expect_countries - filtrados
            if faltan:
                failures.append(f"el SQL no filtra {', '.join(sorted(faltan))}")
            if sobran:
                failures.append(
                    f"el SQL filtra {', '.join(sorted(sobran))}, fuera de lo pedido"
                )

    # La verificacion de la narrativa solo aplica si hubo filas: con cero filas
    # se sirve una plantilla determinista y no hay redaccion que verificar.
    if case.expect_verified_narrative and response.row_count > 0:
        if not response.narrative_verified:
            invalidos = ", ".join(response.unverified_numbers) or "(sin detalle)"
            failures.append(f"la narrativa no quedo verificada; numeros marcados: {invalidos}")
        if not response.narrative:
            failures.append("no hay narrativa")

    return CaseResult(
        case=case,
        ok=not failures,
        failures=failures,
        outcome=response.outcome.value,
        row_count=response.row_count,
        sql=sql,
    )


async def run_cases(settings: Settings, cases: list[Case] | None = None) -> list[CaseResult]:
    selected = cases if cases is not None else CASES
    read_pool = build_read_pool(settings)
    log_pool = build_log_pool(settings)
    await read_pool.open()
    await log_pool.open()
    try:
        executor = ReadOnlyExecutor(read_pool)
        log_executor = LogExecutor(log_pool)
        client = ClaudeClient(api_key=settings.anthropic_api_key)
        system_blocks = build_system_blocks(await load_semantic_dictionary(executor))

        results: list[CaseResult] = []
        for case in selected:
            request = QueryRequest(
                question=case.question,
                countries=case.countries,
                narrative=True,
                history=[
                    ConversationTurn(
                        question=turn.question, countries=turn.countries, sql=turn.sql
                    )
                    for turn in case.history
                ],
            )
            try:
                response = await run_query(
                    request,
                    client=client,
                    executor=executor,
                    log_executor=log_executor,
                    system_blocks=system_blocks,
                    model=settings.sql_model,
                    narrative_model=settings.model_fast,
                    max_rows=settings.max_rows,
                    budget_daily_usd=settings.budget_daily_usd,
                    budget_monthly_usd=settings.budget_monthly_usd,
                    subject_key=EVAL_SUBJECT,
                    prompt_version=settings.prompt_version,
                    app_version=settings.app_version,
                )
            except Exception as err:  # noqa: BLE001 - un caso caido no tumba la corrida
                # Sin esto, un 529 transitorio de la API en el caso 3 tiraba la
                # corrida entera y se perdia lo que ya habian dicho los casos
                # 1 y 2. Cada caso cuesta dinero: no se descartan resultados
                # que ya se pagaron.
                results.append(
                    CaseResult(
                        case=case,
                        ok=False,
                        failures=[f"la consulta fallo: {type(err).__name__}: {err}"],
                        outcome="EXCEPCION",
                        row_count=0,
                        sql=None,
                    )
                )
                continue
            results.append(evaluate(case, response, max_rows=settings.max_rows))
        await wait_for_audit_tasks()
        return results
    finally:
        await read_pool.close()
        await log_pool.close()


def format_report(results: list[CaseResult]) -> str:
    lines = ["", "Preguntas de referencia", "=" * 70]
    for result in results:
        mark = "OK " if result.ok else "FALLA"
        lines.append(f"[{mark:>5}] {result.case.id}")
        lines.append(f'         "{result.case.question}"')
        lines.append(f"         {result.outcome}, {result.row_count} fila(s)")
        for failure in result.failures:
            lines.append(f"         -> {failure}")
        if not result.ok and result.case.regression:
            lines.append(f"         regresion vigilada: {result.case.regression}")
        if not result.ok and result.sql:
            lines.append(f"         SQL: {result.sql}")
    fallas = [r for r in results if not r.ok]
    lines.append("=" * 70)
    lines.append(
        f"{len(results) - len(fallas)}/{len(results)} casos cumplen sus invariantes."
    )
    return "\n".join(lines)


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    results = asyncio.run(run_cases(get_settings()))
    print(format_report(results))
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
