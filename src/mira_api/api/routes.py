from __future__ import annotations

from fastapi import APIRouter, Request

from mira_api.api.schemas import CoverageResponse
from mira_api.db.coverage import fetch_coverage


router = APIRouter()


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(request: Request) -> CoverageResponse:
    """Return exact, precomputed public coverage without invoking a model."""
    return await fetch_coverage(request.app.state.web_pool)
