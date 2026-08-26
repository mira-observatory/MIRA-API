from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Literal
from uuid import uuid4

from mira_api.api.schemas import Column, CoverageNote, QueryRequest, QueryResponse, Warning
from mira_api.audit.outcomes import Outcome
from mira_api.audit.writer import QueryLogRecord, write_audit_log
from mira_api.db.executor import DatabaseError, QueryTimeout, ReadOnlyExecutor, Rows
from mira_api.db.log_executor import LogExecutor
from mira_api.llm.client import ClaudeApiError, ClaudeClient, ClaudeRefusal
from mira_api.nlq.coverage_facts import diagnose_empty_result
from mira_api.nlq.narrative import generate_narrative
from mira_api.nlq.sql_generation import (
    GenerationAttempt,
    GenerationFailed,
    GenerationResult,
    OutOfScope,
    PriorTurn,
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

#: Claves sinteticas de MIRA -- sin significado fuera de esta base, ni
#: siquiera son el numero real del expediente. Verificado contra datos reales
#: (2026-08-26): process_id vale "MIRA-CR-B86D94B96C1A"; el numero real del
#: expediente en SICOP es process_number ("2025XE-000049-0000400001"), una
#: columna distinta que SI se conserva. Mismo caso con supplier_id (un entero
#: interno secuencial: 1, 2, 3...) contra supplier_tax_id (la cedula juridica
#: real, publica). country_code y currency_code tampoco entran aqui: son
#: codigos legibles, no claves de fila.
_INTERNAL_ID_COLUMNS = frozenset(
    {
        "process_id",
        "award_id",
        "item_id",
        "buyer_id",
        "supplier_id",
        "source_item_id",
        "source_award_id",
    }
)


def _strip_internal_ids(result: Rows) -> Rows:
    """Saca las claves internas de MIRA de lo que se le muestra a la persona.

    "MIRA-CR-AWARD-BA79BA102334" no le dice nada a un ciudadano y expone como
    esta armado el esquema interno sin necesidad -- la pregunta nunca es "cual
    es el ID", es sobre el proceso, el monto, la fecha o quien lo gano.

    Se hace aca y no solo ocultando la columna en la tabla del frontend: si se
    dejara en el JSON de la respuesta, cualquiera con las herramientas de
    desarrollador del navegador lo veria igual. Corre antes de que narrativa,
    avisos y auditoria toquen las filas, asi que todo el pipeline ve la misma
    version filtrada -- la narrativa no puede citar un ID que no vio, y no hay
    forma de que la tabla y lo que redacta el modelo queden desincronizados.

    El SQL ejecutado (sql_executed en la respuesta) sigue mostrando los JOIN
    con esas columnas tal cual: la trazabilidad tecnica para quien la quiera
    no se pierde, solo se saca de la tabla pensada para leerse sin SQL.
    """
    columnas = [c for c in result.columns if c not in _INTERNAL_ID_COLUMNS]
    filas = [
        {clave: valor for clave, valor in fila.items() if clave not in _INTERNAL_ID_COLUMNS}
        for fila in result.rows
    ]
    return replace(result, columns=columnas, rows=filas)


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
#: -> narrative -> done/error). GET /query no lo usa; POST /query/stream
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


def unnormalised_item_warning(relations: frozenset[str]) -> Warning | None:
    """Avisa que el nombre de producto viene tal como lo publico la fuente,
    sin categorizar.

    Verificado contra datos reales (2026-08-26): category_normalised esta
    vacia en el 100% de los items, en los 4 paises -- la fuente todavia no
    trae una clasificacion normalizada de productos. Sin ella, "producto" cae
    al texto libre de item_description, que cada portal publica distinto: en
    Honduras, "Ver Pliego" (una etiqueta de la interfaz, no un producto)
    aparece 6,007 veces -- mas que cualquier producto real -- porque varias
    compras distintas usaron ese mismo texto generico en vez de describir lo
    comprado. El numero es real (son filas que de verdad dicen eso), pero
    agruparlas como si fueran "el mismo producto" no lo es.

    No se filtra "Ver Pliego" ni ningun otro texto especifico: manana puede
    ser otro portal con otro texto generico distinto, y adivinar cuales
    excluir es una lista que nunca termina. Se avisa en cambio, igual que con
    monedas mezcladas: la persona decide que tan en serio tomar el resultado.
    """
    if "query.v_items" not in relations:
        return None
    return Warning(
        code="UNNORMALISED_ITEM_TEXT",
        message_es=(
            "Los nombres de producto vienen tal como los publico cada fuente, sin "
            "categorizar todavia: pueden repetirse aunque se trate de compras "
            "distintas -- por ejemplo, cuando la fuente usa un texto generico como "
            '"Ver Pliego" en vez del detalle del producto -- o venir vacios. Tomalo '
            "como una aproximacion, no como una clasificacion exacta."
        ),
    )


def mixed_currency_warning(
    columns: list[Column],
    rows: list[dict[str, object]],
    requested_countries: list[str],
) -> Warning | None:
    """Avisa cuando una tabla de montos no se puede leer como comparacion.

    Los montos de paises distintos vienen en monedas distintas, y no hay tasa
    de cambio en el modelo de datos. Convertir con una inventada seria peor que
    no comparar. Asi que cualquier ranking por monto que cruce paises miente,
    de una de dos formas -- verificadas con datos reales el 2026-08-21 pidiendo
    "las 10 adjudicaciones mas caras" de Costa Rica y Guatemala:

    1. Ordenando por el monto suelto: salen diez contratos costarricenses y
       cero guatemaltecos. No porque los de Guatemala sean chicos, sino porque
       un quetzal vale unos 65 colones, asi que Q3,900,000 (unos 253 millones
       de colones, un contrato grande) queda debajo de cualquier monto en
       colones como numero.
    2. Ordenando por moneda y luego por monto: peor todavia, porque CRC va
       antes que GTQ alfabeticamente y el LIMIT corta antes de llegar a
       Guatemala. Un sesgo perfectamente sistematico.

    En los dos casos la tabla se ve normal y quien la lee no tiene como saber
    que falta un pais entero. Por eso el aviso mira el resultado, no el SQL: da
    igual como se ordeno, lo que importa es si lo que llego representa lo que
    se pregunto.
    """
    if not any(column.kind == "money" for column in columns):
        return None

    monedas = sorted(
        {str(row["currency_code"]) for row in rows if isinstance(row.get("currency_code"), str)}
    )
    presentes = {
        str(row["country_code"]).upper() for row in rows if isinstance(row.get("country_code"), str)
    }
    pedidos = {c.upper() for c in requested_countries}
    faltantes = sorted(pedidos - presentes) if presentes else []

    if len(monedas) >= 2:
        return Warning(
            code="MIXED_CURRENCY",
            message_es=(
                f"La tabla mezcla montos en {', '.join(monedas)}, y no son comparables "
                "entre si: no hay tasa de cambio en los datos. Si esta ordenada por "
                "monto, el orden refleja el numero, no el valor. Conviene preguntar "
                "por un pais a la vez."
            ),
            details={"monedas": monedas},
        )

    if len(pedidos) >= 2 and faltantes:
        return Warning(
            code="MIXED_CURRENCY",
            message_es=(
                f"Se preguntaron varios paises pero la tabla solo trae datos de "
                f"{', '.join(sorted(presentes))}: no aparece {', '.join(faltantes)}. "
                "Los montos de cada pais estan en su propia moneda y no hay tasa de "
                "cambio en los datos, asi que un ranking por monto entre paises deja "
                "fuera a los de moneda mas fuerte. Conviene preguntar por un pais a "
                "la vez."
            ),
            details={"paises_ausentes": faltantes, "paises_presentes": sorted(presentes)},
        )

    return None


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
    sql_max_attempts: int = 3,
    narrative_max_attempts: int = 2,
    narrative_max_rows_in_prompt: int = 25,
    on_event: StreamCallback | None = None,
) -> QueryResponse:
    """Orquesta el pipeline completo: presupuesto -> normalizar -> generar SQL
    -> validar (dentro de generate_validated_sql) -> ejecutar -> redactar y
    verificar (T3.5/T3.6) -> armar la respuesta.

    La redaccion nunca bloquea la respuesta: si el verificador agota sus
    intentos, se sirve una plantilla determinista y narrative_verified queda
    en False, pero las filas ya estan en la respuesta de todas formas.

    `on_event` (Hito 7) reporta las mismas fases segun van quedando listas --
    GET /query lo deja en None (solo le interesa el QueryResponse final),
    POST /query/stream lo usa para transmitir por SSE sin duplicar esta
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
            max_attempts=sql_max_attempts,
            history=[
                PriorTurn(
                    question=turn.question,
                    countries=[c.upper() for c in turn.countries],
                    sql=turn.sql,
                )
                for turn in request.history
            ],
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
    except (ClaudeRefusal, ClaudeApiError):
        # ClaudeApiError cubre la sobrecarga transitoria (529), el limite de
        # tasa y los cortes de red. Sin atraparlo, un 529 -- que ocurre --
        # salia como 500 con traza en vez del FAILED_LLM_ERROR que existe
        # justo para esto.
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
    rows_result = _strip_internal_ids(rows_result)

    columns = _columns_from_rows(rows_result.columns, rows_result.rows)
    _emit(
        "row_count",
        {"row_count": rows_result.row_count, "truncated": rows_result.truncated},
    )
    _emit(
        "rows",
        {"columns": [c.model_dump() for c in columns], "rows": rows_result.rows},
    )

    outcome = Outcome.OK_ZERO_ROWS if rows_result.row_count == 0 else Outcome.OK

    # Un cero nunca se entrega desnudo: se averigua si es un cero real o si
    # simplemente esos datos no estan cargados o el periodo esta fuera de cobertura.
    warnings: list[Warning] = []
    coverage_note: CoverageNote | None = None
    is_zero_aggregate = (
        rows_result.row_count == 1
        and len(rows_result.rows) == 1
        and any(
            str(k).lower()
            in ("count", "total", "total_procesos", "total_contratos", "num_procesos")
            and v == 0
            for k, v in rows_result.rows[0].items()
        )
    )

    if rows_result.row_count == 0 or is_zero_aggregate:
        diagnosis = await diagnose_empty_result(
            executor,
            countries=countries,
            relations=result.validated.relations,
            sql=result.validated.sql,
        )
        warnings = diagnosis.warnings
        coverage_note = diagnosis.coverage

    if rows_result.row_count > 0:
        mezcla = mixed_currency_warning(columns, rows_result.rows, countries)
        if mezcla is not None:
            warnings.append(mezcla)
        sin_normalizar = unnormalised_item_warning(result.validated.relations)
        if sin_normalizar is not None:
            warnings.append(sin_normalizar)

    if rows_result.row_count > 0 and rows_result.truncated:
        # Se alcanzo el tope de filas: lo que se ve es un pedazo, y quien
        # pregunta no tiene como saberlo mirando la tabla. Decirlo importa
        # tanto como los datos -- sacar conclusiones de un pedazo creyendo
        # que es el total es el mismo error que un total mal sumado.
        # append, no asignacion: un resultado puede estar truncado Y mezclar
        # monedas a la vez, y pisar un aviso con el otro deja a medias la unica
        # parte de la respuesta que dice que NO se puede concluir.
        warnings.append(
            Warning(
                code="TRUNCATED_RESULT",
                message_es=(
                    f"Se muestran {rows_result.row_count} filas, que es el maximo por "
                    "consulta: hay mas. Para verlo completo conviene preguntar por un "
                    "mes a la vez."
                ),
                details={"max_rows": rows_result.row_count},
            )
        )
    if warnings:
        # Se emite antes que la narrativa: el motivo del vacio, o el aviso de
        # que falta data por ver, es parte de la respuesta y no un adorno que
        # llega despues.
        _emit("warnings", {"warnings": [w.model_dump() for w in warnings]})

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
            max_attempts=narrative_max_attempts,
            max_rows_in_prompt=narrative_max_rows_in_prompt,
            # Con cero filas o advertencias de cobertura/periodo faltante no se llama
            # al modelo: se sirve la explicacion exacta.
            empty_reason=(
                warnings[0].message_es
                if (
                    warnings
                    and (
                        rows_result.row_count == 0
                        or warnings[0].code in ("PARTIAL_COVERAGE", "NO_DATA_FOR_PERIOD")
                    )
                )
                else None
            ),
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
        warnings=warnings,
        coverage=coverage_note,
        timings_ms=timings_ms,
    )
