from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from mira_api.api.routes import router
from mira_api.api.schemas import EntityCandidate, QueryRequest, QueryResponse
from mira_api.config import get_settings
from mira_api.db.executor import ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.db.pool import build_log_pool, build_read_pool, build_web_pool
from mira_api.llm.client import ClaudeClient
from mira_api.nlq.entities import resolve_entities
from mira_api.nlq.pipeline import run_query
from mira_api.nlq.semantic_dictionary import load_semantic_dictionary
from mira_api.nlq.sql_generation import build_system_blocks
from mira_api.quota.identity import COOKIE_NAME, new_token, resolve_subject_key, sign_token

logger = logging.getLogger(__name__)

# psycopg async no funciona sobre el ProactorEventLoop, el default de Windows
# desde Python 3.8 -- falla en silencio con PoolTimeout, no con un error claro.
# En Linux (produccion, Fly.io) esto es un no-op: SelectorEventLoop ya es el
# default ahi.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.read_pool = build_read_pool(settings)
    app.state.log_pool = build_log_pool(settings)
    # DATABASE_URL_WEB es obligatorio, pero "obligatorio" en pydantic solo exige
    # que la clave exista: una cadena vacia pasa la validacion. Sin esta guarda,
    # ese descuido deja /coverage colgado hasta el timeout del pool en vez de
    # decir que esta mal configurado.
    app.state.web_pool = build_web_pool(settings) if settings.database_url_web else None
    await app.state.read_pool.open()
    await app.state.log_pool.open()
    if app.state.web_pool is not None:
        await app.state.web_pool.open()
    app.state.executor = ReadOnlyExecutor(app.state.read_pool)
    app.state.log_executor = LogExecutor(app.state.log_pool)

    app.state.claude_client = ClaudeClient(api_key=settings.anthropic_api_key)
    # El diccionario semantico se carga una sola vez al arrancar (T3.3): es el
    # contenido estable del prompt de generacion de SQL, y va marcado para
    # cacheo. Un cambio en MIRA-ETL se refleja al reiniciar el servicio, no
    # en caliente -- coherente con que ese diccionario cambia rara vez.
    columns = await load_semantic_dictionary(app.state.executor)
    app.state.sql_system_blocks = build_system_blocks(columns)

    try:
        yield
    finally:
        await app.state.read_pool.close()
        await app.state.log_pool.close()
        if app.state.web_pool is not None:
            await app.state.web_pool.close()


app = FastAPI(
    title="MIRA API",
    description=(
        "Consultas en lenguaje natural sobre contrataciones publicas de Centroamerica. "
        "Todo numero proviene de SQL ejecutado; el modelo de lenguaje solo traduce y redacta."
    ),
    version=get_settings().app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": get_settings().app_version}


@app.get("/entities/resolve")
async def entities_resolve(
    q: str = Query(min_length=1, max_length=200),
    type: Literal["supplier", "buyer"] = Query(...),
    countries: list[str] = Query(min_length=1),
) -> list[EntityCandidate]:
    """Resuelve un nombre a candidatos reales -- sin IA, sqlglot ni el modelo
    de lenguaje tocan esta ruta. Devuelve todos los candidatos con su conteo
    real; nunca fusiona nombres parecidos (caso Karro/Carro)."""
    executor: ReadOnlyExecutor = app.state.executor
    return await resolve_entities(
        executor, query=q, entity_type=type, countries=[c.upper() for c in countries]
    )


def _resolve_subject_key(http_request: Request, *, secret: str) -> tuple[str, str | None]:
    """Solo para atribuir renglones de analytics.query_log a "la misma sesion
    de navegador" -- NUNCA para limitar nada (T5.1/T5.2 siguen inactivos,
    Settings.enable_subject_quota en False). Devuelve (subject_key, cookie a
    emitir); el segundo es None si la cookie que trajo la peticion ya era
    valida y no hace falta reemplazarla."""
    cookie_value = http_request.cookies.get(COOKIE_NAME)
    client_ip = http_request.client.host if http_request.client else None
    subject_key, subject_kind = resolve_subject_key(
        cookie_value=cookie_value, client_ip=client_ip, secret=secret
    )
    if subject_kind == "token":
        return subject_key, None
    return subject_key, sign_token(new_token(), secret=secret)


def _set_subject_cookie(http_response: Response, *, value: str) -> None:
    http_response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        httponly=True,
        secure=True,
        # Ver Settings.cookie_samesite: desplegados en dominios distintos, el
        # navegador no reenvia una cookie "lax" y la atribucion se pierde.
        samesite=get_settings().cookie_samesite,
        max_age=400 * 24 * 3600,
    )


def _resolve_subject_key_for_logging(
    http_request: Request, http_response: Response, *, secret: str
) -> str:
    subject_key, new_cookie_value = _resolve_subject_key(http_request, secret=secret)
    if new_cookie_value is not None:
        _set_subject_cookie(http_response, value=new_cookie_value)
    return subject_key


@app.post("/query")
async def query(
    request: QueryRequest, http_request: Request, http_response: Response
) -> QueryResponse:
    """Traduce una pregunta en espanol a SQL validado, lo ejecuta, devuelve
    filas reales y las redacta en prosa verificada -- pero la tabla es la
    respuesta, el parrafo es un acompanante prescindible."""
    settings = app.state.settings
    subject_key = _resolve_subject_key_for_logging(
        http_request, http_response, secret=settings.token_hmac_secret
    )
    return await run_query(
        request,
        client=app.state.claude_client,
        executor=app.state.executor,
        log_executor=app.state.log_executor,
        system_blocks=app.state.sql_system_blocks,
        model=settings.sql_model,
        narrative_model=settings.model_fast,
        max_rows=settings.max_rows,
        sql_max_attempts=settings.sql_max_attempts,
        narrative_max_attempts=settings.narrative_max_attempts,
        narrative_max_rows_in_prompt=settings.narrative_max_rows_in_prompt,
        budget_daily_usd=settings.budget_daily_usd,
        budget_monthly_usd=settings.budget_monthly_usd,
        subject_key=subject_key,
        prompt_version=settings.prompt_version,
        app_version=settings.app_version,
    )


def _format_sse(event: str, data: dict[str, object]) -> bytes:
    # json.dumps con default=str: outcome es un Outcome (StrEnum, ya serializable),
    # pero por si algun valor futuro no lo es, nunca debe tumbar el stream a mitad.
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@app.post("/query/stream")
async def query_stream(
    request: QueryRequest, http_request: Request
) -> StreamingResponse:
    """Misma traduccion, ejecucion y redaccion que POST /query, pero
    transmitida por SSE segun cada fase queda lista: sql -> row_count -> rows
    -> narrative -> done (o error -> done si el pipeline corta antes).

    La narrativa nunca se transmite token a token: si se hiciera, un texto a
    medio escribir con un numero luego descartado por el verificador (T3.6)
    ya habria llegado al usuario. Se transmite completa, ya verificada o ya
    reemplazada por la plantilla determinista.

    No reutiliza la logica de run_query aparte -- le pasa un callback
    (`on_event`) que encola cada fase; toda la orquestacion (presupuesto, SQL,
    ejecucion, redaccion, auditoria) sigue viviendo en un solo lugar.
    """
    settings = app.state.settings
    # Se resuelve antes de construir la respuesta streaming (que ya no admite
    # response.set_cookie() directo una vez devuelta -- StreamingResponse
    # reemplaza el objeto Response inyectado por FastAPI, no lo complementa).
    subject_key, new_cookie_value = _resolve_subject_key(
        http_request, secret=settings.token_hmac_secret
    )
    streaming_response = StreamingResponse(
        _stream_query_events(request, subject_key), media_type="text/event-stream"
    )
    if new_cookie_value is not None:
        _set_subject_cookie(streaming_response, value=new_cookie_value)
    return streaming_response


async def _stream_query_events(
    request: QueryRequest, subject_key: str
) -> AsyncIterator[bytes]:
    settings = app.state.settings
    queue: asyncio.Queue[tuple[str, dict[str, object]] | None] = asyncio.Queue()

    def on_event(event: str, data: dict[str, object]) -> None:
        queue.put_nowait((event, data))

    async def runner() -> None:
        try:
            await run_query(
                request,
                client=app.state.claude_client,
                executor=app.state.executor,
                log_executor=app.state.log_executor,
                system_blocks=app.state.sql_system_blocks,
                model=settings.sql_model,
                narrative_model=settings.model_fast,
                max_rows=settings.max_rows,
                sql_max_attempts=settings.sql_max_attempts,
                narrative_max_attempts=settings.narrative_max_attempts,
                narrative_max_rows_in_prompt=settings.narrative_max_rows_in_prompt,
                budget_daily_usd=settings.budget_daily_usd,
                budget_monthly_usd=settings.budget_monthly_usd,
                subject_key=subject_key,
                prompt_version=settings.prompt_version,
                app_version=settings.app_version,
                on_event=on_event,
            )
        except Exception:
            logger.exception("fallo inesperado transmitiendo /query/stream")
            on_event("error", {"outcome": "FAILED_LLM_ERROR", "detail": "internal_error"})
            on_event("done", {"outcome": "FAILED_LLM_ERROR", "query_id": None})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(runner())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            event, data = item
            yield _format_sse(event, data)
    finally:
        await task


# Los endpoints restantes se incorporan en fases siguientes:
#   GET /coverage, GET /quota, POST /feedback
