from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mira_api.config import get_settings
from mira_api.db.pool import build_log_pool, build_read_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.read_pool = build_read_pool(settings)
    app.state.log_pool = build_log_pool(settings)
    await app.state.read_pool.open()
    await app.state.log_pool.open()
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


# Los endpoints de consulta se incorporan en la fase 1:
#   POST /v1/query, POST /v1/query/stream, GET /v1/entities/resolve,
#   GET /v1/templates, GET /v1/coverage, GET /v1/quota, POST /v1/feedback
