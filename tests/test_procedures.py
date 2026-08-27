from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from mira_api.db.procedures import PROCEDURES_SQL, _contains_pattern, fetch_procedures


class _Cursor:
    def __init__(self, rows: list[dict[str, object]], total: int = 0) -> None:
        self.rows = rows
        self.total = total
        self.sql = ""
        self.params: dict[str, object] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, sql: str, params: dict[str, object]) -> None:
        self.sql = sql
        self.params = params

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    async def fetchone(self) -> dict[str, object]:
        return {"total_count": self.total}


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def cursor(self, **_kwargs) -> _Cursor:
        return self._cursor


class _Pool:
    def __init__(self, rows: list[dict[str, object]], total: int = 0) -> None:
        self.cursor = _Cursor(rows, total)

    def connection(self) -> _Connection:
        return _Connection(self.cursor)


def _row() -> dict[str, object]:
    return {
        "process_id": "GT-123",
        "process_number": "NOG-123",
        "country_code": "GT",
        "title": "Compra de medicamentos",
        "description": None,
        "procurement_method": "Cotizacion",
        "process_status": "OPEN",
        "source_status": "Publicado",
        "publication_date": datetime(2026, 8, 20, tzinfo=UTC),
        "closing_date": None,
        "estimated_amount": Decimal("125.50"),
        "currency_code": "GTQ",
        "source_system": "guatecompras",
        "source_url": "https://example.test/123",
        "data_quality_status": "COMPLETE",
        "total_count": 51,
    }


@pytest.mark.asyncio
async def test_filters_and_paginates_with_bound_parameters() -> None:
    pool = _Pool([_row()])
    result = await fetch_procedures(  # type: ignore[arg-type]
        pool,
        q="100% seguro_",
        countries=["GT"],
        statuses=["OPEN"],
        procurement_methods=["Cotizacion"],
        published_from=date(2026, 1, 1),
        published_to=date(2026, 12, 31),
        page=2,
        page_size=25,
    )

    assert result.total == 51
    assert result.total_pages == 3
    assert result.items[0].process_number == "NOG-123"
    assert pool.cursor.params["q_pattern"] == r"%100\% seguro\_%"
    assert pool.cursor.params["procurement_methods"] == ["%Cotizacion%"]
    assert pool.cursor.params["offset"] == 25
    assert "100% seguro_" not in pool.cursor.sql


@pytest.mark.asyncio
async def test_out_of_range_page_keeps_real_total() -> None:
    result = await fetch_procedures(  # type: ignore[arg-type]
        _Pool([], total=51),
        q=None,
        countries=[],
        statuses=[],
        procurement_methods=[],
        published_from=None,
        published_to=None,
        page=4,
        page_size=25,
    )
    assert result.items == []
    assert result.total == 51
    assert result.total_pages == 3


def test_sql_is_fixed_and_search_escapes_wildcards() -> None:
    assert "query.v_process" in PROCEDURES_SQL
    assert "%(countries)s" in PROCEDURES_SQL
    assert all(word not in PROCEDURES_SQL.lower() for word in ("delete ", "update ", "insert "))
    assert _contains_pattern(r"a_b%c\d") == r"%a\_b\%c\\d%"
