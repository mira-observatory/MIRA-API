from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal, TypeVar

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from mira_api.api.schemas import (
    CoverageCountry,
    CoverageResponse,
    CoverageSource,
    CoverageSummary,
)


# This statement is deliberately constant. Public coverage never goes through
# the generated-SQL validator or any language-model component.
COVERAGE_SQL = """
    select
        source_key,
        country_code,
        source_system,
        display_name,
        status,
        process_count,
        buyer_count,
        supplier_count,
        publication_date_min,
        publication_date_max,
        complete_process_count,
        partial_process_count,
        process_without_date_count,
        last_successful_load_at,
        refreshed_at,
        sort_order
    from web.coverage_sources
    where status in ('ACTIVE', 'PLANNED')
    order by sort_order, country_code, source_key
"""

DateValue = TypeVar("DateValue", date, datetime)


async def fetch_coverage(pool: AsyncConnectionPool) -> CoverageResponse:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(COVERAGE_SQL)
            rows = await cursor.fetchall()

    return build_coverage_response(rows)


def build_coverage_response(rows: list[dict[str, Any]]) -> CoverageResponse:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["country_code"]), []).append(row)

    countries: list[CoverageCountry] = []
    for country_code, country_rows in grouped.items():
        active = [row for row in country_rows if row["status"] == "ACTIVE"]
        country_status: Literal["ACTIVE", "PLANNED"] = (
            "ACTIVE" if active else "PLANNED"
        )
        sources = [_source_from_row(row) for row in country_rows]
        countries.append(
            CoverageCountry(
                country_code=country_code,
                status=country_status,
                active_sources=len(active),
                process_count=sum(int(row["process_count"]) for row in active),
                buyer_count=sum(int(row["buyer_count"]) for row in active),
                supplier_count=sum(int(row["supplier_count"]) for row in active),
                coverage_from=_minimum(row["publication_date_min"] for row in active),
                coverage_to=_maximum(row["publication_date_max"] for row in active),
                last_successful_load_at=_maximum(
                    row["last_successful_load_at"] for row in active
                ),
                sources=sources,
            )
        )

    active_countries = [country for country in countries if country.status == "ACTIVE"]
    return CoverageResponse(
        summary=CoverageSummary(
            active_countries=len(active_countries),
            planned_countries=sum(
                country.status == "PLANNED" for country in countries
            ),
            active_sources=sum(country.active_sources for country in active_countries),
            process_count=sum(country.process_count for country in active_countries),
            coverage_from=_minimum(country.coverage_from for country in active_countries),
            coverage_to=_maximum(country.coverage_to for country in active_countries),
            last_successful_load_at=_maximum(
                country.last_successful_load_at for country in active_countries
            ),
        ),
        countries=countries,
    )


def _source_from_row(row: dict[str, Any]) -> CoverageSource:
    return CoverageSource(
        source_key=str(row["source_key"]),
        source_system=str(row["source_system"]),
        display_name=str(row["display_name"]),
        status=row["status"],
        process_count=int(row["process_count"]),
        buyer_count=int(row["buyer_count"]),
        supplier_count=int(row["supplier_count"]),
        coverage_from=row["publication_date_min"],
        coverage_to=row["publication_date_max"],
        complete_process_count=int(row["complete_process_count"]),
        partial_process_count=int(row["partial_process_count"]),
        process_without_date_count=int(row["process_without_date_count"]),
        last_successful_load_at=row["last_successful_load_at"],
        refreshed_at=row["refreshed_at"],
    )


def _minimum(values: Iterable[DateValue | None]) -> DateValue | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _maximum(values: Iterable[DateValue | None]) -> DateValue | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None
