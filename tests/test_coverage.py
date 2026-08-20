from __future__ import annotations

from datetime import UTC, date, datetime

from mira_api.db.coverage import COVERAGE_SQL, build_coverage_response


def _row(
    source_key: str,
    country_code: str,
    status: str,
    process_count: int = 0,
) -> dict[str, object]:
    active = status == "ACTIVE"
    return {
        "source_key": source_key,
        "country_code": country_code,
        "source_system": source_key,
        "display_name": source_key,
        "status": status,
        "process_count": process_count,
        "buyer_count": 3 if active else 0,
        "supplier_count": 4 if active else 0,
        "publication_date_min": date(2021, 1, 1) if active else None,
        "publication_date_max": date(2026, 8, 10) if active else None,
        "complete_process_count": process_count if active else 0,
        "partial_process_count": 0,
        "process_without_date_count": 0,
        "last_successful_load_at": (
            datetime(2026, 8, 18, tzinfo=UTC) if active else None
        ),
        "refreshed_at": datetime(2026, 8, 18, tzinfo=UTC),
        "sort_order": 0,
    }


def test_builds_summary_and_country_breakdown() -> None:
    result = build_coverage_response(
        [
            _row("cr_primary", "CR", "ACTIVE", 7),
            _row("cr_secondary", "CR", "ACTIVE", 8),
            _row("sv_planned", "SV", "PLANNED"),
        ]
    )

    assert result.summary.active_countries == 1
    assert result.summary.planned_countries == 1
    assert result.summary.active_sources == 2
    assert result.summary.process_count == 15
    assert result.summary.coverage_from == date(2021, 1, 1)
    assert len(result.countries) == 2
    assert result.countries[0].process_count == 15
    assert result.countries[1].status == "PLANNED"


def test_handles_empty_coverage() -> None:
    result = build_coverage_response([])

    assert result.summary.active_countries == 0
    assert result.summary.process_count == 0
    assert result.summary.coverage_from is None
    assert result.countries == []


def test_uses_constant_public_sql_outside_query_schema() -> None:
    assert "web.coverage_sources" in COVERAGE_SQL
    assert "query." not in COVERAGE_SQL
    assert "mart." not in COVERAGE_SQL
