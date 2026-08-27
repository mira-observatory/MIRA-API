from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import StringConstraints

from mira_api.api.schemas import CoverageResponse, ProceduresResponse, ProcessStatus
from mira_api.db.coverage import fetch_coverage
from mira_api.db.procedures import fetch_procedures

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
                "La cobertura publica no esta configurada: falta DATABASE_URL_WEB (rol mira_web)."
            ),
        )
    return await fetch_coverage(web_pool)


CountryCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$"),
]
ProcurementMethod = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]


@router.get("/procedures", response_model=ProceduresResponse)
async def get_procedures(
    request: Request,
    q: Annotated[str | None, Query(min_length=2, max_length=200)] = None,
    country: Annotated[list[CountryCode] | None, Query(max_length=10)] = None,
    status: Annotated[list[ProcessStatus] | None, Query(max_length=10)] = None,
    procurement_method: Annotated[list[ProcurementMethod] | None, Query(max_length=20)] = None,
    published_from: date | None = None,
    published_to: date | None = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ProceduresResponse:
    """Lista y filtra procedimientos con SQL fijo, sin consumir IA."""
    if published_from and published_to and published_from > published_to:
        raise HTTPException(
            status_code=422,
            detail="published_from no puede ser posterior a published_to.",
        )

    return await fetch_procedures(
        request.app.state.read_pool,
        q=q,
        countries=list(dict.fromkeys(country or [])),
        statuses=list(dict.fromkeys(status or [])),
        procurement_methods=list(dict.fromkeys(procurement_method or [])),
        published_from=published_from,
        published_to=published_to,
        page=page,
        page_size=page_size,
    )
