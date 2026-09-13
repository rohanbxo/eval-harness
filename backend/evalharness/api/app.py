"""FastAPI application wiring (SPEC 9.2).

Every route lives under ``/api``. Response models are declared explicitly on each
route because the dashboard generates its TypeScript from this OpenAPI schema --
a loosely typed response here becomes an ``any`` over there.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from evalharness import __version__
from evalharness.api.routes import attempts, health, leaderboard, models, runs, scenarios
from evalharness.config import get_settings
from evalharness.db.session import close_database, create_database, set_database

logger = logging.getLogger(__name__)

#: The dashboard's dev server. No auth and no multi-tenancy in v1 (SPEC 1).
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


def allowed_origins() -> list[str]:
    """Dev origins plus whatever the deployment adds.

    A deployed dashboard is served from its own domain and its client components
    fetch the API from the browser, so that origin has to be allowed explicitly.
    Kept additive: a deployment never loses the local dev origins.
    """
    settings = get_settings()
    extra = [origin for origin in settings.extra_cors_origins if origin not in ALLOWED_ORIGINS]
    return [*ALLOWED_ORIGINS, *extra]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine for the app's event loop.

    The engine is created here rather than at import time because an asyncpg pool
    is bound to the loop that created it, and uvicorn's loop does not exist yet
    when this module is imported.
    """
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    set_database(create_database(settings.database_url))
    try:
        yield
    finally:
        await close_database()


def create_app() -> FastAPI:
    app = FastAPI(
        title="EvalHarness API",
        version=__version__,
        description=(
            "Runs agentic tool-use scenarios against any LiteLLM-supported model "
            "and grades them deterministically."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (health, scenarios, models, runs, attempts, leaderboard):
        app.include_router(module.router, prefix="/api")

    return app


app = create_app()
