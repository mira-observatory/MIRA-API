from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from mira_api.audit.outcomes import Outcome
from mira_api.db.log_executor import TRANSIENT_LOG_ERRORS, LogExecutor, LogTransaction
from mira_api.nlq.sql_generation import GenerationAttempt

_INSERT_QUERY_LOG_SQL = """
    insert into analytics.query_log
        (subject_key, question_text, response_text, outcome, attempt_count,
         total_latency_ms, prompt_version, app_version, model_used,
         query_id, error_stage, error_type)
    values
        (%(subject_key)s, %(question_text)s, %(response_text)s, %(outcome)s,
         %(attempt_count)s, %(total_latency_ms)s, %(prompt_version)s, %(app_version)s,
         %(model_used)s, %(query_id)s, %(error_stage)s, %(error_type)s)
    on conflict (query_id) do update set query_id = excluded.query_id
    returning id
"""

_INSERT_QUERY_ATTEMPT_SQL = """
    insert into analytics.query_attempt
        (query_log_id, attempt_number, generated_sql, outcome, rejection_rule,
         rejection_detail, row_count)
    values
        (%(query_log_id)s, %(attempt_number)s, %(generated_sql)s, %(outcome)s,
         %(rejection_rule)s, %(rejection_detail)s, %(row_count)s)
    on conflict (query_log_id, attempt_number) do update set
        generated_sql = excluded.generated_sql,
        outcome = excluded.outcome,
        rejection_rule = excluded.rejection_rule,
        rejection_detail = excluded.rejection_detail,
        row_count = excluded.row_count
"""


@dataclass(frozen=True)
class QueryLogRecord:
    """Lo que se sabe al terminar una peticion, sin importar en que punto del
    pipeline termino (bloqueada por presupuesto, rechazada, fallida u OK)."""

    subject_key: str
    question_text: str
    response_text: str | None
    outcome: Outcome
    attempt_count: int
    total_latency_ms: int
    prompt_version: str
    app_version: str
    model_used: str
    query_id: UUID = field(default_factory=uuid4)
    error_stage: str | None = None
    error_type: str | None = None


async def write_query_log(
    log_executor: LogExecutor | LogTransaction, record: QueryLogRecord
) -> int:
    row = await log_executor.fetch_one(
        _INSERT_QUERY_LOG_SQL,
        {
            "subject_key": record.subject_key,
            "question_text": record.question_text,
            "response_text": record.response_text,
            "outcome": record.outcome.value,
            "attempt_count": record.attempt_count,
            "total_latency_ms": record.total_latency_ms,
            "prompt_version": record.prompt_version,
            "app_version": record.app_version,
            "model_used": record.model_used,
            "query_id": record.query_id,
            "error_stage": record.error_stage,
            "error_type": record.error_type,
        },
    )
    assert row is not None
    return int(row["id"])


async def write_query_attempts(
    log_executor: LogExecutor | LogTransaction,
    *,
    query_log_id: int,
    attempts: list[GenerationAttempt],
    final_outcome: Outcome,
    final_row_count: int | None,
) -> None:
    """Un renglon por intento de generacion. El intento aceptado (si lo hay)
    no trae su propio outcome -- sql_generation.py no sabe todavia si la
    ejecucion o la redaccion salieron bien, asi que se completa aqui con el
    outcome final de la peticion completa."""
    for attempt in attempts:
        outcome = attempt.outcome if attempt.outcome is not None else final_outcome
        row_count = final_row_count if attempt.accepted else None
        await log_executor.execute(
            _INSERT_QUERY_ATTEMPT_SQL,
            {
                "query_log_id": query_log_id,
                "attempt_number": attempt.attempt_no,
                "generated_sql": attempt.sql_text,
                "outcome": outcome.value,
                "rejection_rule": attempt.rejection_rule,
                "rejection_detail": attempt.rejection_detail,
                "row_count": row_count,
            },
        )


async def write_audit_log(
    log_executor: LogExecutor,
    *,
    record: QueryLogRecord,
    attempts: list[GenerationAttempt],
    final_row_count: int | None,
) -> None:
    """Confirma padre e intentos juntos. Reintentos transitorios idempotentes
    por query_id, incluso si se perdio la conexion al recibir el COMMIT."""
    for attempt_no in range(3):
        try:
            async with log_executor.transaction() as transaction:
                query_log_id = await write_query_log(transaction, record)
                await write_query_attempts(
                    transaction,
                    query_log_id=query_log_id,
                    attempts=attempts,
                    final_outcome=record.outcome,
                    final_row_count=final_row_count,
                )
            return
        except TRANSIENT_LOG_ERRORS:
            if attempt_no == 2:
                raise
            await asyncio.sleep(0.05 * (attempt_no + 1))
