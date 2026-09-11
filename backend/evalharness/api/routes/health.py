"""Liveness probe (Phase 0 acceptance: ``/api/health`` returns ok)."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import text

from evalharness import __version__
from evalharness.api.schemas import HealthResponse
from evalharness.db.session import get_database

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, operation_id="getHealth")
async def health() -> HealthResponse:
    """Report process health, plus whether the database answers.

    The endpoint stays 200 even when Postgres is down: compose starts the API
    before migrations have necessarily settled, and a health check that fails
    then would take the container down for a recoverable condition.
    """
    database = "ok"
    try:
        async with get_database().session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        LOGGER.warning("health check could not reach the database: %s", exc)
        database = "unavailable"
    return HealthResponse(status="ok", version=__version__, database=database)
