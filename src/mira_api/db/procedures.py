from __future__ import annotations

from datetime import date
from math import ceil
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from mira_api.api.schemas import (
    Procedure,
    ProcedureFilters,
    ProceduresResponse,
    ProcessStatus,
)

PROCEDURE_FILTER_SQL = """
    where (%(countries)s::text[] is null or p.country_code = any(%(countries)s::text[]))
      and (%(statuses)s::text[] is null or p.process_status = any(%(statuses)s::text[]))
      and (
        %(procurement_methods)s::text[] is null
        or p.procurement_method ilike any(%(procurement_methods)s::text[])
      )
      and (
        %(published_from)s::date is null
        or p.publication_date >= %(published_from)s::date
      )
      and (
        %(published_to)s::date is null
        or p.publication_date < (%(published_to)s::date + interval '1 day')
      )
      and (
        %(q)s::text is null
        or p.process_number ilike %(q_pattern)s escape '\\'
        or p.title ilike %(q_pattern)s escape '\\'
        or p.description ilike %(q_pattern)s escape '\\'
      )
"""

# Consulta constante: los filtros viajan ligados por psycopg y nunca se
# interpolan en el SQL. No interviene el modelo ni el validador de SQL generado.
PROCEDURES_SQL = f"""
    select
        p.process_id, p.process_number, p.country_code, p.title, p.description,
        p.procurement_method, p.process_status, p.source_status,
        p.publication_date, p.closing_date, p.estimated_amount, p.currency_code,
        p.source_system, p.source_url, p.data_quality_status,
        count(*) over () as total_count
    from query.v_process p
    {PROCEDURE_FILTER_SQL}
    order by p.publication_date desc nulls last, p.process_id
    limit %(page_size)s offset %(offset)s
"""

PROCEDURES_COUNT_SQL = f"""
    select count(*) as total_count
    from query.v_process p
    {PROCEDURE_FILTER_SQL}
"""


async def fetch_procedures(
    pool: AsyncConnectionPool,
    *,
    q: str | None,
    countries: list[str],
    statuses: list[ProcessStatus],
    procurement_methods: list[str],
    published_from: date | None,
    published_to: date | None,
    page: int,
    page_size: int,
) -> ProceduresResponse:
    clean_q = (q.strip() or None) if q else None
    params: dict[str, Any] = {
        "q": clean_q,
        "q_pattern": _contains_pattern(clean_q) if clean_q else None,
        "countries": countries or None,
        "statuses": statuses or None,
        "procurement_methods": (
            [_contains_pattern(method) for method in procurement_methods]
            if procurement_methods
            else None
        ),
        "published_from": published_from,
        "published_to": published_to,
        "page_size": page_size,
        "offset": (page - 1) * page_size,
    }

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(PROCEDURES_SQL, params)
        rows = await cursor.fetchall()
        total = int(rows[0]["total_count"]) if rows else 0

        # Una pagina que queda fuera del rango no contiene la ventana count(*).
        if not rows and page > 1:
            await cursor.execute(PROCEDURES_COUNT_SQL, params)
            count_row = await cursor.fetchone()
            total = int(count_row["total_count"]) if count_row is not None else 0

    return ProceduresResponse(
        items=[Procedure.model_validate(_without_total(row)) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
        filters=ProcedureFilters(
            q=clean_q,
            countries=countries,
            statuses=statuses,
            procurement_methods=procurement_methods,
            published_from=published_from,
            published_to=published_to,
        ),
    )


def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _without_total(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "total_count"}
