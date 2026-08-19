from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from mira_api.api.schemas import EntityCandidate, QueryRequest, QueryResponse
from mira_api.config import get_settings
from mira_api.db.executor import ReadOnlyExecutor
from mira_api.db.log_executor import LogExecutor
from mira_api.db.pool import build_log_pool, build_read_pool
from mira_api.llm.client import ClaudeClient
from mira_api.nlq.entities import resolve_entities
from mira_api.nlq.pipeline import run_query
from mira_api.nlq.semantic_dictionary import load_semantic_dictionary
from mira_api.nlq.sql_generation import build_system_blocks

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
    await app.state.read_pool.open()
    await app.state.log_pool.open()
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": get_settings().app_version}


@app.get("/v1/entities/resolve")
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


@app.post("/v1/query")
async def query(request: QueryRequest) -> QueryResponse:
    """Traduce una pregunta en espanol a SQL validado, lo ejecuta, y devuelve
    filas reales. Sin redaccion todavia (T3.5/T3.6): los datos se entregan
    solos, que es exactamente lo que el plan pide que pase aunque no haya
    parrafo."""
    settings = app.state.settings
    return await run_query(
        request,
        client=app.state.claude_client,
        executor=app.state.executor,
        log_executor=app.state.log_executor,
        system_blocks=app.state.sql_system_blocks,
        model=settings.sql_model,
        max_rows=settings.max_rows,
        budget_daily_usd=settings.budget_daily_usd,
        budget_monthly_usd=settings.budget_monthly_usd,
    )


# Los endpoints restantes se incorporan en fases siguientes:
#   POST /v1/query/stream, GET /v1/coverage, GET /v1/quota, POST /v1/feedback
