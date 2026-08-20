"""Comprueba que la base cumple el contrato que la API necesita.

Pensado para correrse **despues de una recarga del ETL**. Una recarga puede
dejar los datos perfectos y aun asi romper el servicio: si se recrean los
esquemas, los permisos de mira_query y mira_logger se pierden, y esos permisos
se otorgan a mano (MIRA-ETL, docs/database_security.md), no desde sql/.

No llama al modelo de lenguaje: no cuesta nada y tarda segundos.

    python -m mira_api.evals.contract

Sale con codigo 1 si alguna comprobacion falla, para poder encadenarlo.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from mira_api.audit.outcomes import Outcome
from mira_api.config import Settings, get_settings
from mira_api.db.executor import DatabaseError, ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.db.pool import build_log_pool, build_read_pool
from mira_api.nlq.validator import ALLOWED_RELATIONS
from mira_api.quota.counters import read_counter, record_spend

#: Sujeto de la sonda de escritura. Es una clave primaria natural
#: (subject_key, period_type, period_key), asi que correr esto mil veces
#: actualiza la misma fila en vez de acumular basura.
PROBE_SUBJECT = "CONTRACT-CHECK"

#: Ninguna adjudicacion centroamericana llega a mil millones de dolares o
#: euros. Si aparecen, es el bug de moneda: montos en colones etiquetados
#: USD/EUR porque el monto salia de MONTO_ADJU_LINEA_CRC y la moneda de
#: MONEDA_ADJUDICADA (corregido en MIRA-ETL ba9f9d3).
IMPLAUSIBLE_AMOUNT = 1_000_000_000


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    #: Informativo: se muestra pero no hace fallar la corrida.
    advisory: bool = False


async def _views_readable(executor: ReadOnlyExecutor) -> CheckResult:
    """Cada vista de la lista blanca tiene que ser legible por mira_query. Si
    falla una, el modelo generara SQL correcto que la base rechaza."""
    unreadable: list[str] = []
    for relation in sorted(ALLOWED_RELATIONS):
        try:
            await executor.run(f"select 1 from {relation} limit 1", max_rows=1)
        except DatabaseError as err:
            unreadable.append(f"{relation} ({type(err).__name__})")
    if unreadable:
        return CheckResult(
            "mira_query lee las vistas permitidas",
            False,
            f"no puede leer: {', '.join(unreadable)}",
        )
    return CheckResult(
        "mira_query lee las vistas permitidas", True, f"{len(ALLOWED_RELATIONS)} vistas"
    )


async def _mart_is_denied(executor: ReadOnlyExecutor) -> CheckResult:
    """La frontera de seguridad real no es el validador, es el permiso: aunque
    el validador tuviera un agujero, mira_query no debe poder tocar mart.*."""
    try:
        await executor.run("select 1 from mart.processes limit 1", max_rows=1)
    except DatabaseError:
        return CheckResult("mira_query NO alcanza mart.*", True, "acceso denegado, como debe ser")
    return CheckResult(
        "mira_query NO alcanza mart.*",
        False,
        "mira_query puede leer mart.processes -- la separacion de esquemas se perdio",
    )


async def _unaccent_available(executor: ReadOnlyExecutor) -> CheckResult:
    """La resolucion de entidades la usa para buscar sin acentos. Vive en
    `query` y no en `mart` justamente para que mira_query la alcance."""
    try:
        rows = await executor.run("select query.f_unaccent('Ñandú') as v", max_rows=1)
    except DatabaseError as err:
        return CheckResult("query.f_unaccent disponible", False, f"{type(err).__name__}: {err}")
    value = rows.rows[0]["v"] if rows.rows else None
    return CheckResult("query.f_unaccent disponible", value == "Nandu", f"'Ñandú' -> {value!r}")


async def _dictionary_covers_allowlist(executor: ReadOnlyExecutor) -> CheckResult:
    """Sin diccionario el prompt se queda sin descripciones de columnas y la
    generacion de SQL se degrada en silencio. Lo siembra
    MIRA-ETL/sql/003_semantic_dictionary.sql, que hay que re-correr tras una
    recarga que recree los esquemas."""
    try:
        rows = await executor.run(
            "select view_name, count(*) as n from query.semantic_dictionary"
            " group by view_name",
            max_rows=100,
        )
    except DatabaseError as err:
        return CheckResult("diccionario semantico poblado", False, f"{type(err).__name__}: {err}")

    documented = {str(row["view_name"]) for row in rows.rows}
    missing = ALLOWED_RELATIONS - documented
    total = sum(int(row["n"]) for row in rows.rows)
    if missing:
        return CheckResult(
            "diccionario semantico poblado",
            False,
            f"sin documentar: {', '.join(sorted(missing))}",
        )
    return CheckResult(
        "diccionario semantico poblado",
        True,
        f"las {len(ALLOWED_RELATIONS)} vistas permitidas documentadas ({total} columnas en total)",
    )


async def _audit_is_writable(log_executor: LogExecutor) -> CheckResult:
    """La escritura de auditoria es deliberadamente fire-and-forget: si el
    permiso se pierde, falla en silencio y nadie se entera hasta que hace
    falta el registro. Se sondea con un UPSERT sobre una clave fija."""
    try:
        before = await read_counter(log_executor, subject_key=PROBE_SUBJECT, period_type="DAY")
        after = await record_spend(
            log_executor, subject_key=PROBE_SUBJECT, period_type="DAY", cost_usd=0.0
        )
    except DatabaseError as err:
        return CheckResult(
            "mira_logger escribe en analytics", False, f"{type(err).__name__}: {err}"
        )
    grew = after.query_count == before.query_count + 1
    return CheckResult(
        "mira_logger escribe en analytics",
        grew,
        f"contador de sonda {before.query_count} -> {after.query_count}",
    )


async def _outcome_check_accepts_taxonomy(executor: ReadOnlyExecutor) -> CheckResult:
    """El CHECK de la base y la taxonomia del codigo tienen que coincidir. Si
    la base rechaza un valor, ese resultado nunca queda registrado -- y los
    codigos que hay que vigilar son justo los raros."""
    try:
        rows = await executor.run(
            """
            select rel.relname as tabla, pg_get_constraintdef(con.oid) as def
            from pg_constraint con
            join pg_class rel on rel.oid = con.conrelid
            join pg_namespace n on n.oid = rel.relnamespace
            where n.nspname = 'analytics'
              and rel.relname in ('query_log', 'query_attempt')
              and con.contype = 'c'
            """,
            max_rows=20,
        )
    except DatabaseError as err:
        return CheckResult("el CHECK de outcome cubre la taxonomia", False, str(err))

    taxonomy = {o.value for o in Outcome}
    problems: list[str] = []
    checked = 0
    for row in rows.rows:
        definition = str(row["def"])
        if "outcome" not in definition:
            continue
        checked += 1
        missing = sorted(value for value in taxonomy if f"'{value}'" not in definition)
        if missing:
            problems.append(f"{row['tabla']} no acepta {', '.join(missing)}")
    if checked == 0:
        return CheckResult(
            "el CHECK de outcome cubre la taxonomia",
            False,
            "no hay CHECK sobre outcome en analytics.query_log/query_attempt",
        )
    if problems:
        return CheckResult("el CHECK de outcome cubre la taxonomia", False, "; ".join(problems))
    return CheckResult(
        "el CHECK de outcome cubre la taxonomia",
        True,
        f"{len(taxonomy)} valores en {checked} tablas",
    )


async def _amounts_are_plausible(executor: ReadOnlyExecutor) -> CheckResult:
    """Regresion del bug de moneda. Un contrato centroamericano de mil millones
    de dolares no existe: si aparece, son colones mal etiquetados y el chat va
    a afirmar cifras infladas unas 500 veces."""
    try:
        rows = await executor.run(
            """
            select count(*) as n, coalesce(max(awarded_amount), 0) as mayor
            from query.v_awards
            where currency_code in ('USD', 'EUR')
              and awarded_amount > %(limite)s
            """,
            max_rows=1,
            params={"limite": IMPLAUSIBLE_AMOUNT},
        )
    except DatabaseError as err:
        return CheckResult("montos USD/EUR plausibles", False, f"{type(err).__name__}: {err}")

    row = rows.rows[0] if rows.rows else {"n": 0, "mayor": 0}
    count = int(row["n"])
    if count:
        return CheckResult(
            "montos USD/EUR plausibles",
            False,
            f"{count} adjudicaciones sobre {IMPLAUSIBLE_AMOUNT:,}; la mayor es {row['mayor']}",
        )
    return CheckResult("montos USD/EUR plausibles", True, "ninguna sobre mil millones")


async def _country_coverage(executor: ReadOnlyExecutor) -> CheckResult:
    """Informativo: que paises tienen datos de verdad. No falla, pero es lo
    primero que hay que mirar despues de una recarga."""
    try:
        rows = await executor.run(
            "select country_code, count(*) as n from query.v_process"
            " group by country_code order by country_code",
            max_rows=50,
        )
    except DatabaseError as err:
        return CheckResult("paises con datos", False, f"{type(err).__name__}: {err}", advisory=True)

    if not rows.rows:
        return CheckResult("paises con datos", False, "query.v_process esta vacia", advisory=True)
    resumen = ", ".join(f"{row['country_code']}={row['n']}" for row in rows.rows)
    return CheckResult("paises con datos", True, resumen, advisory=True)


async def run_contract_checks(settings: Settings) -> list[CheckResult]:
    read_pool = build_read_pool(settings)
    log_pool = build_log_pool(settings)
    await read_pool.open()
    await log_pool.open()
    try:
        executor = ReadOnlyExecutor(read_pool)
        log_executor = LogExecutor(log_pool)
        return [
            await _views_readable(executor),
            await _mart_is_denied(executor),
            await _unaccent_available(executor),
            await _dictionary_covers_allowlist(executor),
            await _audit_is_writable(log_executor),
            await _outcome_check_accepts_taxonomy(executor),
            await _amounts_are_plausible(executor),
            await _country_coverage(executor),
        ]
    finally:
        await read_pool.close()
        await log_pool.close()


def format_report(results: list[CheckResult]) -> str:
    lines = ["", "Contrato con la base de datos", "=" * 60]
    for result in results:
        if result.advisory:
            mark = "i"
        else:
            mark = "OK " if result.ok else "FALLA"
        lines.append(f"[{mark:>5}] {result.name}")
        lines.append(f"         {result.detail}")
    fallas = [r for r in results if not r.ok and not r.advisory]
    lines.append("=" * 60)
    if fallas:
        lines.append(f"{len(fallas)} comprobacion(es) fallaron.")
        lines.append("Si se acaba de recrear la base, revisar los GRANT de")
        lines.append("MIRA-ETL/docs/database_security.md y re-correr sql/*.sql.")
    else:
        lines.append("La base cumple el contrato que necesita la API.")
    return "\n".join(lines)


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    results = asyncio.run(run_contract_checks(get_settings()))
    print(format_report(results))
    return 1 if any(not r.ok and not r.advisory for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
