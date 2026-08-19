from __future__ import annotations

import time
from typing import Literal
from uuid import uuid4

from mira_api.api.schemas import Column, QueryRequest, QueryResponse
from mira_api.audit.outcomes import Outcome
from mira_api.db.executor import DatabaseError, QueryTimeout, ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.llm.client import ClaudeClient, ClaudeRefusal
from mira_api.nlq.sql_generation import (
    GenerationFailed,
    GenerationResult,
    OutOfScope,
    Usage,
    generate_validated_sql,
)
from mira_api.quota.budget import check_budget, record_global_spend
from mira_api.quota.pricing import compute_cost_usd

#: Columnas cuyo nombre indica dinero -- ninguna vista tiene una columna de
#: tipo "money" real, es siempre numeric + currency_code aparte (Parte 1.6:
#: "No existe columna de monto unificada, y es a proposito").
_MONEY_COLUMNS = {"estimated_amount", "awarded_amount"}
_DATE_COLUMNS_SUFFIXES = ("_date", "_at")


def normalise_question(question: str) -> str:
    """Recorte a 400 caracteres, colapso de espacios -- primera defensa de
    costo antes de que la pregunta toque al modelo."""
    collapsed = " ".join(question.split())
    return collapsed[:400]


def _infer_column_kind(name: str) -> Literal["number", "money", "date", "text"]:
    if name in _MONEY_COLUMNS:
        return "money"
    if name.endswith(_DATE_COLUMNS_SUFFIXES) or name == "period":
        return "date"
    if name.endswith(("_id", "_count")) or name in {"row_count"}:
        return "number"
    return "text"


def _columns_from_rows(names: list[str], rows: list[dict[str, object]]) -> list[Column]:
    columns = []
    for name in names:
        kind = _infer_column_kind(name)
        currency_code: str | None = None
        if kind == "money" and rows:
            value = rows[0].get("currency_code")
            currency_code = value if isinstance(value, str) else None
        columns.append(Column(name=name, kind=kind, currency_code=currency_code))
    return columns


async def _charge_global_budget(
    log_executor: LogExecutor, *, model: str, usage: Usage
) -> float:
    cost_usd = compute_cost_usd(
        model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
    )
    await record_global_spend(log_executor, cost_usd=cost_usd)
    return cost_usd


async def run_query(
    request: QueryRequest,
    *,
    client: ClaudeClient,
    executor: ReadOnlyExecutor,
    log_executor: LogExecutor,
    system_blocks: list[dict[str, object]],
    model: str,
    max_rows: int,
    budget_daily_usd: float,
    budget_monthly_usd: float,
) -> QueryResponse:
    """Orquesta el pipeline completo: presupuesto -> normalizar -> generar SQL
    -> validar (dentro de generate_validated_sql) -> ejecutar -> armar la
    respuesta.

    No redacta ni verifica narrativa todavia (T3.5/T3.6) -- los datos se
    entregan solos, que es exactamente lo que el plan pide que pase incluso
    cuando la redaccion no existe.
    """
    query_id = uuid4()
    question = normalise_question(request.question)
    countries = [c.upper() for c in request.countries]
    timings_ms: dict[str, int] = {}

    # T5.3: la cuota se consume ANTES de llamar al modelo. Este chequeo es
    # contra lo YA gastado en llamadas anteriores -- no cuesta nada llamarlo.
    budget = await check_budget(
        log_executor, daily_limit_usd=budget_daily_usd, monthly_limit_usd=budget_monthly_usd
    )
    if budget.blocked:
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="out_of_scope",
            outcome=Outcome.THROTTLED_BUDGET,
            countries_filter=countries,
            timings_ms=timings_ms,
        )

    llm_start = time.monotonic()
    try:
        result: GenerationResult = await generate_validated_sql(
            client,
            model=model,
            system=system_blocks,
            question=question,
            countries=countries,
            max_rows=max_rows,
        )
    except OutOfScope as out_of_scope:
        timings_ms["llm_ms"] = int((time.monotonic() - llm_start) * 1000)
        await _charge_global_budget(log_executor, model=model, usage=out_of_scope.usage)
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="out_of_scope",
            outcome=Outcome.OUT_OF_SCOPE,
            countries_filter=countries,
            timings_ms=timings_ms,
        )
    except GenerationFailed as failed:
        timings_ms["llm_ms"] = int((time.monotonic() - llm_start) * 1000)
        await _charge_global_budget(log_executor, model=model, usage=failed.usage)
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="generated_sql",
            outcome=failed.outcome,
            countries_filter=countries,
            timings_ms=timings_ms,
        )
    except ClaudeRefusal:
        timings_ms["llm_ms"] = int((time.monotonic() - llm_start) * 1000)
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="generated_sql",
            outcome=Outcome.FAILED_LLM_ERROR,
            countries_filter=countries,
            timings_ms=timings_ms,
        )
    timings_ms["llm_ms"] = int((time.monotonic() - llm_start) * 1000)
    await _charge_global_budget(log_executor, model=model, usage=result.usage)

    db_start = time.monotonic()
    try:
        rows_result = await executor.run(result.validated.sql, max_rows=max_rows)
    except QueryTimeout:
        timings_ms["db_ms"] = int((time.monotonic() - db_start) * 1000)
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="generated_sql",
            outcome=Outcome.FAILED_DB_TIMEOUT,
            sql_executed=result.validated.sql,
            countries_filter=countries,
            timings_ms=timings_ms,
        )
    except DatabaseError:
        timings_ms["db_ms"] = int((time.monotonic() - db_start) * 1000)
        return QueryResponse(
            query_id=query_id,
            question=question,
            strategy="generated_sql",
            outcome=Outcome.FAILED_DB_ERROR,
            sql_executed=result.validated.sql,
            countries_filter=countries,
            timings_ms=timings_ms,
        )
    timings_ms["db_ms"] = int((time.monotonic() - db_start) * 1000)

    outcome = Outcome.OK_ZERO_ROWS if rows_result.row_count == 0 else Outcome.OK
    return QueryResponse(
        query_id=query_id,
        question=question,
        strategy="generated_sql",
        outcome=outcome,
        sql_executed=result.validated.sql,
        countries_filter=countries,
        columns=_columns_from_rows(rows_result.columns, rows_result.rows),
        rows=rows_result.rows,
        row_count=rows_result.row_count,
        truncated=rows_result.truncated,
        timings_ms=timings_ms,
    )
