from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from mira_api.api.schemas import CoverageResponse
from mira_api.db.coverage import fetch_coverage

router = APIRouter()


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(request: Request) -> CoverageResponse:
    """Return exact, precomputed public coverage without invoking a model."""
    web_pool = request.app.state.web_pool
    if web_pool is None:
        # Sin DATABASE_URL_WEB no hay a que conectarse. Un 503 explicito es
        # mejor que dejar la peticion colgada contra un DSN vacio hasta el
        # timeout del pool.
        raise HTTPException(
            status_code=503,
            detail=(
                "La cobertura publica no esta configurada: falta DATABASE_URL_WEB "
                "(rol mira_web)."
            ),
        )
    return await fetch_coverage(web_pool)
