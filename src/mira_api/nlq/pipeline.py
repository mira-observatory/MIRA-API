from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from mira_api.api.schemas import Column, QueryRequest, QueryResponse
from mira_api.audit.outcomes import Outcome
from mira_api.audit.writer import QueryLogRecord, write_audit_log
from mira_api.db.executor import DatabaseError, QueryTimeout, ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.llm.client import ClaudeClient, ClaudeRefusal
from mira_api.nlq.narrative import generate_narrative
from mira_api.nlq.sql_generation import (
    GenerationAttempt,
    GenerationFailed,
    GenerationResult,
    OutOfScope,
    Usage,
    generate_validated_sql,
)
from mira_api.quota.budget import check_budget, record_global_spend
from mira_api.quota.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

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
    # OJO: *_id no es "number" -- process_id/award_id/etc. son codigos de texto
    # como "MIRA-CR-AWARD-BA79BA102334", no enteros. Tratarlos como numero le
    # hacia perder el dato al frontend (esperaba un number de JS) y les
    # hubiera puesto separador de miles, que no tiene sentido en un id.
    if name.endswith("_count") or name == "row_count":
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


#: Se llama en cada transicion de fase del pipeline (sql -> row_count -> rows
#: -> narrative -> done/error). GET /v1/query no lo usa; POST /v1/query/stream
#: (Hito 7) lo conecta a una cola SSE. Nunca es async: solo encola, nunca
#: bloquea al pipeline por un consumidor lento.
StreamCallback = Callable[[str, dict[str, object]], None]


#: Referencias fuertes a las tareas de auditoria en curso -- sin esto, asyncio
#: puede recolectar la tarea a mitad de camino porque nada mas la referencia
#: (la unica referencia que crea create_task() es debil).
_audit_tasks: set[asyncio.Task[None]] = set()


def _on_audit_task_done(task: asyncio.Task[None]) -> None:
    _audit_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("fallo al escribir el registro de auditoria", exc_info=error)


async def wait_for_audit_tasks() -> None:
    """Solo para pruebas: la escritura de auditoria es fire-and-forget en
    produccion (no debe sumarle latencia a la respuesta), pero una prueba que
    quiere inspeccionar lo que se escribio necesita esperar a que termine."""
    pending = [t for t in _audit_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _schedule_audit_write(
    log_executor: LogExecutor,
    *,
    record: QueryLogRecord,
    attempts: list[GenerationAttempt],
    final_row_count: int | None = None,
) -> None:
    """Nunca se espera (Hito 4): la auditoria no le suma latencia a la
    respuesta del usuario. Un fallo aqui se registra pero nunca cambia lo que
    ya se le devolvio."""
    task = asyncio.create_task(
        write_audit_log(
            log_executor, record=record, attempts=attempts, final_row_count=final_row_count
        )
    )
    _audit_tasks.add(task)
    task.add_done_callback(_on_audit_task_done)


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
    narrative_model: str,
    max_rows: int,
    budget_daily_usd: float,
    budget_monthly_usd: float,
    subject_key: str,
    prompt_version: str,
    app_version: str,
    on_event: StreamCallback | None = None,
) -> QueryResponse:
    """Orquesta el pipeline completo: presupuesto -> normalizar -> generar SQL
    -> validar (dentro de generate_validated_sql) -> ejecutar -> redactar y
    verificar (T3.5/T3.6) -> armar la respuesta.

    La redaccion nunca bloquea la respuesta: si el verificador la descarta dos
    veces, se sirve una plantilla determinista y narrative_verified queda en
    False, pero las filas ya estan en la respuesta de todas formas.

    `on_event` (Hito 7) reporta las mismas fases segun van quedando listas --
    GET /v1/query lo deja en None (solo le interesa el QueryResponse final),
    POST /v1/query/stream lo usa para transmitir por SSE sin duplicar esta
    logica en dos sitios.
    """
    query_id = uuid4()
    question = normalise_question(request.question)
    countries = [c.upper() for c in request.countries]
    timings_ms: dict[str, int] = {}

    def _emit(event: str, data: dict[str, object]) -> None:
        if on_event is not None:
            on_event(event, data)

    def _record(
        outcome: Outcome, *, response_text: str | None, attempt_count: int
    ) -> QueryLogRecord:
        return QueryLogRecord(
            subject_key=subject_key,
            question_text=question,
            response_text=response_text,
            outcome=outcome,
            attempt_count=attempt_count,
            total_latency_ms=sum(timings_ms.values()),
            prompt_version=prompt_version,
            app_version=app_version,
            model_used=model,
        )

    # T5.3: la cuota se consume ANTES de llamar al modelo. Este chequeo es
    # contra lo YA gastado en llamadas anteriores -- no cuesta nada llamarlo.
    budget = await check_budget(
        log_executor, daily_limit_usd=budget_daily_usd, monthly_limit_usd=budget_monthly_usd
    )
    if budget.blocked:
        _schedule_audit_write(
            log_executor,
            record=_record(Outcome.THROTTLED_BUDGET, response_text=None, attempt_count=0),
            attempts=[],
        )
        _emit("error", {"outcome": Outcome.THROTTLED_BUDGET.value, "detail": budget.reason})
        _emit("done", {"outcome": Outcome.THROTTLED_BUDGET.value, "query_id": str(query_id)})
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
        _schedule_audit_write(
            log_executor,
            record=_record(
                Outcome.OUT_OF_SCOPE, response_text=None, attempt_count=len(out_of_scope.attempts)
            ),
            attempts=out_of_scope.attempts,
        )
        _emit("error", {"outcome": Outcome.OUT_OF_SCOPE.value, "detail": None})
        _emit("done", {"outcome": Outcome.OUT_OF_SCOPE.value, "query_id": str(query_id)})
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
        _schedule_audit_write(
            log_executor,
            record=_record(failed.outcome, response_text=None, attempt_count=len(failed.attempts)),
            attempts=failed.attempts,
        )
        _emit("error", {"outcome": failed.outcome.value, "detail": failed.detail})
        _emit("done", {"outcome": failed.outcome.value, "query_id": str(query_id)})
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
        _schedule_audit_write(
            log_executor,
            record=_record(Outcome.FAILED_LLM_ERROR, response_text=None, attempt_count=0),
            attempts=[],
        )
        _emit("error", {"outcome": Outcome.FAILED_LLM_ERROR.value, "detail": None})
        _emit("done", {"outcome": Outcome.FAILED_LLM_ERROR.value, "query_id": str(query_id)})
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
    _emit("sql", {"sql": result.validated.sql})

    db_start = time.monotonic()
    try:
        rows_result = await executor.run(result.validated.sql, max_rows=max_rows)
    except QueryTimeout:
        timings_ms["db_ms"] = int((time.monotonic() - db_start) * 1000)
        _schedule_audit_write(
            log_executor,
            record=_record(
                Outcome.FAILED_DB_TIMEOUT, response_text=None, attempt_count=len(result.attempts)
            ),
            attempts=result.attempts,
        )
        _emit("error", {"outcome": Outcome.FAILED_DB_TIMEOUT.value, "detail": None})
        _emit("done", {"outcome": Outcome.FAILED_DB_TIMEOUT.value, "query_id": str(query_id)})
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
        _schedule_audit_write(
            log_executor,
            record=_record(
                Outcome.FAILED_DB_ERROR, response_text=None, attempt_count=len(result.attempts)
            ),
            attempts=result.attempts,
        )
        _emit("error", {"outcome": Outcome.FAILED_DB_ERROR.value, "detail": None})
        _emit("done", {"outcome": Outcome.FAILED_DB_ERROR.value, "query_id": str(query_id)})
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

    columns = _columns_from_rows(rows_result.columns, rows_result.rows)
    _emit("row_count", {"row_count": rows_result.row_count, "truncated": rows_result.truncated})
    _emit(
        "rows",
        {"columns": [c.model_dump() for c in columns], "rows": rows_result.rows},
    )

    outcome = Outcome.OK_ZERO_ROWS if rows_result.row_count == 0 else Outcome.OK

    narrative_text: str | None = None
    narrative_verified = False
    unverified_numbers: list[str] = []
    if request.narrative:
        narrative_start = time.monotonic()
        narrative_result = await generate_narrative(
            client,
            model=narrative_model,
            question=question,
            rows=rows_result.rows,
            row_count=rows_result.row_count,
            truncated=rows_result.truncated,
        )
        timings_ms["narrative_ms"] = int((time.monotonic() - narrative_start) * 1000)
        await _charge_global_budget(
            log_executor, model=narrative_model, usage=narrative_result.usage
        )
        narrative_text = narrative_result.text
        narrative_verified = narrative_result.verified
        unverified_numbers = narrative_result.unverified_numbers
        # Metrica bloqueante (Parte 1.12): datos entregados igual, pero la
        # redaccion se reemplazo por la plantilla porque alucino un numero.
        if outcome is Outcome.OK and not narrative_verified:
            outcome = Outcome.OK_DEGRADED_NARRATIVE
        _emit(
            "narrative",
            {
                "text": narrative_text,
                "verified": narrative_verified,
                "unverified_numbers": unverified_numbers,
            },
        )

    _schedule_audit_write(
        log_executor,
        record=_record(outcome, response_text=narrative_text, attempt_count=len(result.attempts)),
        attempts=result.attempts,
        final_row_count=rows_result.row_count,
    )
    _emit("done", {"outcome": outcome.value, "query_id": str(query_id)})

    return QueryResponse(
        query_id=query_id,
        question=question,
        strategy="generated_sql",
        outcome=outcome,
        sql_executed=result.validated.sql,
        countries_filter=countries,
        columns=columns,
        rows=rows_result.rows,
        row_count=rows_result.row_count,
        truncated=rows_result.truncated,
        narrative=narrative_text,
        narrative_verified=narrative_verified,
        unverified_numbers=unverified_numbers,
        timings_ms=timings_ms,
    )
