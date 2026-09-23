"""Real PostgreSQL COMMIT/rollback checks using session-local temporary tables.

MIRA_TEST_AUDIT_DB_URL may point to a database where TEMP is allowed: no
permanent tables, logs, quotas, roles or schemas are written by this test.
The DDL comes from the sibling MIRA-ETL checkout (or MIRA_ETL_SQL_DIR).
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from mira_api.api.schemas import QueryRequest
from mira_api.audit.outcomes import Outcome
from mira_api.audit.writer import QueryLogRecord, write_audit_log
from mira_api.db.log_executor import LogExecutor
from mira_api.llm.client import ClaudeRefusal
from mira_api.nlq.pipeline import run_query
from mira_api.nlq.sql_generation import GenerationAttempt
from tests.test_pipeline import _ScriptedClient, _ScriptedExecutor

DSN = os.environ.get("MIRA_TEST_AUDIT_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="requires MIRA_TEST_AUDIT_DB_URL (TEMP only)")


class _SingleConnectionPool:
    def __init__(self, connection):
        self.connection_value = connection

    @asynccontextmanager
    async def connection(self):
        yield self.connection_value


class _TemporaryTransaction:
    def __init__(self, transaction):
        self.transaction = transaction

    async def fetch_one(self, sql, params=None):
        return await self.transaction.fetch_one(sql.replace("analytics.", "pg_temp."), params)

    async def execute(self, sql, params=None):
        return await self.transaction.execute(sql.replace("analytics.", "pg_temp."), params)


class _TemporaryLogExecutor(LogExecutor):
    @asynccontextmanager
    async def transaction(self):
        async with super().transaction() as transaction:
            yield _TemporaryTransaction(transaction)

    async def fetch_one(self, sql, params=None):
        # The real pipeline checks/charges quotas. Keep those in memory so
        # running this test never consumes the application's actual budget.
        assert "analytics.quota_counters" in sql
        return {"query_count": 0, "spent_usd": 0.0}


class _RefusingClient:
    async def complete_text(self, **kwargs):
        raise ClaudeRefusal(None, "test refusal")


@pytest.mark.asyncio
async def test_pipeline_errors_commit_distinct_outcomes_and_atomic_attempts():
    sql_dir = Path(os.environ.get("MIRA_ETL_SQL_DIR", Path(__file__).parents[2] / "MIRA-ETL/sql"))
    schema = (sql_dir / "001_init.sql").read_text()
    async with await psycopg.AsyncConnection.connect(
        DSN, autocommit=True, connect_timeout=10
    ) as connection:
        for table in ("query_log", "query_attempt"):
            match = re.search(
                rf"create table if not exists analytics\.{table} \(.*?;", schema, re.S
            )
            assert match
            ddl = (
                match[0]
                .replace(
                    f"create table if not exists analytics.{table}",
                    f"create temporary table {table}",
                )
                .replace("references analytics.", "references pg_temp.")
            )
            await connection.execute(ddl)
        executor = _TemporaryLogExecutor(_SingleConnectionPool(connection))  # type: ignore[arg-type]
        valid_sql = "select awarded_amount from query.v_awards"
        cases = [
            (
                _ScriptedClient([valid_sql]),
                psycopg.errors.QueryCanceled("test"),
                Outcome.FAILED_DB_TIMEOUT,
                "query_execution",
            ),
            (
                _ScriptedClient([valid_sql]),
                psycopg.errors.UndefinedColumn("test"),
                Outcome.FAILED_DB_ERROR,
                "query_execution",
            ),
            (_RefusingClient(), None, Outcome.FAILED_LLM_ERROR, "sql_generation"),
            (
                _ScriptedClient(["select * from mart.private"] * 3),
                None,
                Outcome.REJECTED_SQL_RELATION,
                "sql_generation",
            ),
            (
                _ScriptedClient([valid_sql]),
                RuntimeError("test"),
                Outcome.FAILED_INTERNAL_ERROR,
                "query_execution",
            ),
            (
                _ScriptedClient(["QUESTION_TOO_BROAD"]),
                None,
                Outcome.REJECTED_QUESTION_TOO_BROAD,
                "sql_generation",
            ),
            (
                _ScriptedClient(["INTENT_UNCLEAR"]),
                None,
                Outcome.REJECTED_INTENT_UNCLEAR,
                "sql_generation",
            ),
        ]
        for client, error, expected, stage in cases:
            response = await run_query(
                QueryRequest(question="audit integration probe", countries=["GT"], narrative=False),
                client=client,
                executor=_ScriptedExecutor(error=error),  # type: ignore[arg-type]
                log_executor=executor,
                system_blocks=[],
                model="claude-sonnet-5",
                narrative_model="claude-haiku-4-5-20251001",
                max_rows=10,
                budget_daily_usd=1000,
                budget_monthly_usd=1000,
                subject_key="TEMP-AUDIT-PROBE",
                prompt_version="test",
                app_version="test",
            )
            assert response.outcome is expected
            assert connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            row = await (
                await connection.execute(
                    "select outcome, error_stage, attempt_count "
                    "from pg_temp.query_log where query_id=%s",
                    (response.query_id,),
                )
            ).fetchone()
            assert row is not None and row[:2] == (expected.value, stage)
            count = await (
                await connection.execute(
                    "select count(*) from pg_temp.query_attempt a join pg_temp.query_log l "
                    "on l.id=a.query_log_id where l.query_id=%s",
                    (response.query_id,),
                )
            ).fetchone()
            assert count[0] == row[2]

        record = QueryLogRecord(
            subject_key="TEMP-AUDIT-PROBE",
            question_text="idempotence",
            response_text=None,
            outcome=Outcome.FAILED_DB_ERROR,
            attempt_count=1,
            total_latency_ms=1,
            prompt_version="test",
            app_version="test",
            model_used="test",
        )
        attempts = [GenerationAttempt(1, valid_sql, True)]
        for _ in range(2):
            await write_audit_log(executor, record=record, attempts=attempts, final_row_count=None)
        count = await (
            await connection.execute(
                "select count(*) from pg_temp.query_log where query_id=%s",
                (record.query_id,),
            )
        ).fetchone()
        assert count == (1,)

        record = replace(record, query_id=uuid4())
        with pytest.raises(psycopg.errors.NotNullViolation):
            await write_audit_log(
                executor,
                record=record,
                attempts=[GenerationAttempt(None, valid_sql, True)],  # type: ignore[arg-type]
                final_row_count=None,
            )
        count = await (
            await connection.execute(
                "select count(*) from pg_temp.query_log where query_id=%s",
                (record.query_id,),
            )
        ).fetchone()
        assert count == (0,), "a failed attempt write must roll back its parent"
