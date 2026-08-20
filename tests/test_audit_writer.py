from __future__ import annotations

import pytest

from mira_api.audit.outcomes import Outcome
from mira_api.audit.writer import QueryLogRecord, write_audit_log, write_query_log
from mira_api.nlq.sql_generation import GenerationAttempt


class _FakeLogExecutor:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, dict]] = []
        self.execute_calls: list[tuple[str, dict]] = []
        self._next_id = 1

    async def fetch_one(self, sql: str, params: dict | None = None) -> dict | None:
        self.fetch_calls.append((sql, params or {}))
        row = {"id": self._next_id}
        self._next_id += 1
        return row

    async def execute(self, sql: str, params: dict | None = None) -> None:
        self.execute_calls.append((sql, params or {}))


def _record(**overrides: object) -> QueryLogRecord:
    defaults: dict = {
        "subject_key": "sub-1",
        "question_text": "cuantos procesos hay en CR",
        "response_text": "Se encontraron 7992 procesos.",
        "outcome": Outcome.OK,
        "attempt_count": 1,
        "total_latency_ms": 1200,
        "prompt_version": "0.1.0",
        "app_version": "0.1.0",
        "model_used": "claude-sonnet-5",
    }
    defaults.update(overrides)
    return QueryLogRecord(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_query_log_devuelve_el_id_generado() -> None:
    log_executor = _FakeLogExecutor()

    query_log_id = await write_query_log(log_executor, _record())  # type: ignore[arg-type]

    assert query_log_id == 1
    sql, params = log_executor.fetch_calls[0]
    assert "analytics.query_log" in sql
    assert params["subject_key"] == "sub-1"
    assert params["outcome"] == "OK"


@pytest.mark.asyncio
async def test_write_audit_log_completa_el_outcome_del_intento_aceptado() -> None:
    """El intento aceptado no trae su propio Outcome (sql_generation.py no
    sabe si la ejecucion salio bien) -- write_audit_log lo completa con el
    outcome final de la peticion, y el numero de filas real."""
    log_executor = _FakeLogExecutor()
    attempts = [GenerationAttempt(1, "select 1", accepted=True)]

    await write_audit_log(
        log_executor,  # type: ignore[arg-type]
        record=_record(outcome=Outcome.OK, attempt_count=1),
        attempts=attempts,
        final_row_count=42,
    )

    assert len(log_executor.execute_calls) == 1
    _, params = log_executor.execute_calls[0]
    assert params["outcome"] == "OK"
    assert params["row_count"] == 42
    assert params["rejection_rule"] is None


@pytest.mark.asyncio
async def test_write_audit_log_conserva_el_outcome_propio_de_intentos_rechazados() -> None:
    log_executor = _FakeLogExecutor()
    attempts = [
        GenerationAttempt(
            1,
            "select * from mart.processes",
            accepted=False,
            rejection_rule="forbidden_schema",
            rejection_detail="mart.processes",
            outcome=Outcome.REJECTED_SQL_RELATION,
        ),
        GenerationAttempt(
            2,
            "select * from mart.processes",
            accepted=False,
            rejection_rule="forbidden_schema",
            rejection_detail="mart.processes",
            outcome=Outcome.REJECTED_SQL_RELATION,
        ),
    ]

    await write_audit_log(
        log_executor,  # type: ignore[arg-type]
        record=_record(outcome=Outcome.REJECTED_SQL_RELATION, attempt_count=2),
        attempts=attempts,
        final_row_count=None,
    )

    assert len(log_executor.execute_calls) == 2
    for _, params in log_executor.execute_calls:
        assert params["outcome"] == "REJECTED_SQL_RELATION"
        assert params["rejection_rule"] == "forbidden_schema"
        assert params["row_count"] is None


@pytest.mark.asyncio
async def test_write_audit_log_sin_intentos_no_escribe_query_attempt() -> None:
    log_executor = _FakeLogExecutor()

    await write_audit_log(
        log_executor,  # type: ignore[arg-type]
        record=_record(outcome=Outcome.THROTTLED_BUDGET, response_text=None, attempt_count=0),
        attempts=[],
        final_row_count=None,
    )

    assert len(log_executor.fetch_calls) == 1
    assert log_executor.execute_calls == []
