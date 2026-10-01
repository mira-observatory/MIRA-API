"""Regression queries for award lists and buyer rankings with supplier data.

SQLite exercises relational semantics on synthetic data; PostgreSQL plans and
timings still require verification against PostgreSQL.
"""

from __future__ import annotations

import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from mira_api.api.schemas import QueryRequest
from mira_api.audit.outcomes import Outcome
from mira_api.db.executor import Rows
from mira_api.nlq.pipeline import run_query, wait_for_audit_tasks
from mira_api.nlq.prompts import SQL_SYSTEM_PROMPT
from mira_api.nlq.ranking import asks_supplier_total
from mira_api.nlq.sql_generation import generate_validated_sql
from mira_api.nlq.validator import validate
from tests.test_pipeline import _FakeLogExecutor
from tests.test_sql_generation import _ScriptedClient

AWARDS_QUESTION = (
    "Muéstrame las 5 adjudicaciones de medicamentos más caras de Guatemala, "
    "con institución compradora, proveedor, monto y moneda."
)
BUYERS_QUESTION = (
    "En Guatemala durante 2025, ¿cuáles fueron las 5 instituciones con más "
    "adjudicaciones de medicamentos registradas en quetzales? Para cada institución, "
    "muestra la cantidad de adjudicaciones distintas, la cantidad de proveedores "
    "distintos y el monto de su adjudicación individual más alta. Ordena por "
    "cantidad de adjudicaciones y excluye las canceladas."
)


def example(start: str, end: str) -> str:
    return start + SQL_SYSTEM_PROMPT.split(start, 1)[1].split(end, 1)[0].strip()


AWARDS_SQL = example("WITH top_awards AS (", "\n6f.")
BUYERS_SQL = example("WITH top_buyers AS (", "\n7.")


@pytest.mark.parametrize("question,sql", [
    (AWARDS_QUESTION, AWARDS_SQL), (BUYERS_QUESTION, BUYERS_SQL),
    ("Show the 5 most expensive medication awards with buyer, supplier, amount and currency.",
     AWARDS_SQL),
])
@pytest.mark.asyncio
async def test_accepts_question_without_forcing_supplier_totals(question: str, sql: str) -> None:
    assert not asks_supplier_total(question)
    client = _ScriptedClient([sql])
    result = await generate_validated_sql(
        client, model="test", system=[], question=question, countries=["GT"], max_rows=500,
    )
    assert len(result.attempts) == 1
    assert result.validated.effective_limit == 5
    assert "query.v_supplier_award_totals" not in result.validated.relations


@pytest.fixture
def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ':memory:' AS query")
    conn.executescript("""
        CREATE TABLE query.v_process
            (process_id TEXT PRIMARY KEY, country_code TEXT, title TEXT, description TEXT);
        CREATE TABLE query.source_awards
            (award_id TEXT PRIMARY KEY, process_id TEXT, awarded_amount NUMERIC,
             currency_code TEXT, award_date TEXT, award_status TEXT);
        CREATE VIEW query.v_awards AS SELECT * FROM source_awards
            WHERE award_status IS NULL OR award_status = 'active';
        CREATE TABLE query.v_buyers (buyer_id INTEGER PRIMARY KEY, name_normalised TEXT);
        CREATE TABLE query.v_suppliers (supplier_id INTEGER PRIMARY KEY, name_normalised TEXT);
        CREATE TABLE query.v_process_buyers
            (process_id TEXT, buyer_id INTEGER, PRIMARY KEY (process_id, buyer_id));
        CREATE TABLE query.v_award_suppliers
            (award_id TEXT, supplier_id INTEGER, PRIMARY KEY (award_id, supplier_id));
    """)
    conn.executemany("INSERT INTO query.v_process VALUES (?, ?, ?, ?)", [
        (f"p{i}", "CR" if i == 10 else "GT",
         "Computadoras" if i == 9 else "MEDICAMENTO", None)
        for i in range(1, 11)
    ])
    conn.executemany("INSERT INTO query.source_awards VALUES (?, ?, ?, ?, ?, ?)", [
        ("a1", "p1", 500, "GTQ", "2025-01-01", "active"),
        ("a2", "p1", 300, "GTQ", "2025-12-31", "active"),
        ("a3", "p2", 400, "GTQ", "2025-02-01", None),
        ("a4", "p3", 200, "GTQ", "2025-02-01", "active"),
        ("a5", "p4", 100, "GTQ", "2025-02-01", "active"),
        ("a6", "p5", 50, "GTQ", "2025-02-01", "active"),
        ("a7", "p6", 2000, "GTQ", "2025-02-01", "cancelled"),
        ("a8", "p7", 900, "GTQ", "2026-01-01", "active"),
        ("a9", "p8", 800, "USD", "2025-02-01", "active"),
        ("a10", "p9", 5000, "GTQ", "2025-02-01", "active"),
        ("a11", "p10", 7000, "CRC", "2025-02-01", "active"),
    ])
    conn.executemany("INSERT INTO query.v_buyers VALUES (?, ?)", [
        (i, f"Institucion {i}") for i in range(1, 6)
    ])
    conn.executemany("INSERT INTO query.v_suppliers VALUES (?, ?)", [
        (i, f"Proveedor {i}") for i in range(1, 5)
    ])
    conn.executemany("INSERT INTO query.v_process_buyers VALUES (?, ?)", [
        ("p1", 1), ("p1", 2), ("p2", 1), ("p3", 3), ("p4", 4), ("p5", 5),
        *[(f"p{i}", 1) for i in range(6, 11)],
    ])
    conn.executemany("INSERT INTO query.v_award_suppliers VALUES (?, ?)", [
        ("a1", 1), ("a1", 2), ("a2", 1), ("a2", 2),
        ("a4", 3), ("a5", 3), ("a6", 4),
    ])
    yield conn
    conn.close()


def evaluate(conn: sqlite3.Connection, sql: str, question: str) -> list[dict]:
    validated = validate(sql, max_rows=500, countries=["GT"], question=question)
    tree = sqlglot.parse_one(validated.sql, read="postgres")
    # SQLite transpilation does not support PostgreSQL's aggregate ORDER BY;
    # only presentation of names changes, not membership, ranking or counts.
    for aggregate in tree.find_all(exp.GroupConcat):
        if isinstance(aggregate.this, exp.Order):
            aggregate.set("this", aggregate.this.this)
    return [dict(row) for row in conn.execute(tree.sql(dialect="sqlite"))]


def test_five_distinct_awards_despite_multiple_buyers_and_suppliers(database) -> None:
    rows = evaluate(database, AWARDS_SQL, AWARDS_QUESTION)
    assert [r["award_id"] for r in rows] == ["a8", "a9", "a1", "a3", "a2"]
    assert rows[2]["buyer_name"] == "Institucion 1, Institucion 2"
    assert rows[2]["supplier_name"] == "Proveedor 1, Proveedor 2"
    assert rows[3]["supplier_name"] is None  # keep awards without supplier data
    assert rows[1]["currency_code"] == "USD"  # never assume an unrequested currency


def test_buyer_ranking_counts_distinct_awards_and_keeps_null_status(database) -> None:
    rows = evaluate(database, BUYERS_SQL, BUYERS_QUESTION)
    assert rows == [
        {"buyer_name": f"Institucion {i}", "award_count": count,
         "supplier_count": suppliers, "max_awarded_amount": amount, "currency_code": "GTQ"}
        for i, count, suppliers, amount in [
            (1, 3, 2, 500), (2, 2, 2, 500), (3, 1, 1, 200), (4, 1, 1, 100), (5, 1, 1, 50),
        ]
    ]


@pytest.mark.parametrize("question,sql", [
    (AWARDS_QUESTION, AWARDS_SQL), (BUYERS_QUESTION, BUYERS_SQL),
])
@pytest.mark.asyncio
async def test_pipeline_returns_five_rows_in_json_stream_and_audit(database, question, sql) -> None:
    class Executor:
        async def run(self, sql: str, *, max_rows: int, params=None) -> Rows:
            assert params is None
            data = evaluate(database, sql, question)
            return Rows(columns=list(data[0]), rows=data, row_count=len(data), truncated=False)

    events: list[tuple[str, dict]] = []
    audit = _FakeLogExecutor()
    response = await run_query(
        QueryRequest(question=question, countries=["GT"], narrative=False),
        client=_ScriptedClient([sql]),  # type: ignore[arg-type]
        executor=Executor(),  # type: ignore[arg-type]
        log_executor=audit,  # type: ignore[arg-type]
        system_blocks=[], model="claude-sonnet-5", narrative_model="claude-haiku-4-5-20251001",
        max_rows=500, budget_daily_usd=1000, budget_monthly_usd=1000,
        subject_key="test-medications", prompt_version="test", app_version="test",
        on_event=lambda name, payload: events.append((name, payload)),
    )
    await wait_for_audit_tasks()
    assert response.outcome is Outcome.OK
    assert response.row_count == 5
    assert response.model_dump(mode="json")["outcome"] == "OK"
    assert not any(name == "error" for name, _ in events)
    assert events[-1][0] == "done"
    assert events[-1][1]["outcome"] == "OK"
    assert audit.query_log_rows[0]["outcome"] == "OK"
    assert audit.query_attempt_rows[0]["outcome"] == "OK"
