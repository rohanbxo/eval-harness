"""The read-only deployment flag (EVALHARNESS_READ_ONLY).

The public demo serves a frozen snapshot of one four-model run and holds no model
credentials, so it must refuse to launch or cancel anything. 405 rather than 503:
the method is not supported at all, and a retry can never succeed.

Both directions are tested. A guard that only ever runs with the flag on cannot
tell "the write was refused" from "the write was broken anyway", so
`test_the_writes_succeed_with_the_flag_off` launches the same run for real and
asserts it is accepted. If that test ever fails, the 405 tests below stop meaning
anything.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "scenarios"
# The fixture registry: the real one holds only models under evaluation.
MODELS_FILE = Path(__file__).resolve().parent / "data" / "fake_models.yaml"

_ENV_KEYS = (
    "DATABASE_URL",
    "REDIS_URL",
    "EVALHARNESS_SCENARIOS_DIR",
    "EVALHARNESS_MODELS_FILE",
    "EVALHARNESS_CELERY_ALWAYS_EAGER",
    "EVALHARNESS_READ_ONLY",
)

LAUNCH = {
    "model_key": "fake",
    "scenario_ids": ["travel-booking"],
    "k": 1,
    "transcript": "golden",
    # Test runs are throwaway; the dirty-tree guard is covered in test_manifest.py.
    "allow_dirty": True,
}


@contextlib.contextmanager
def _configured(db_path: Path, *, read_only: bool) -> Iterator[str]:
    """Point the stack at a temp SQLite file, with the flag set either way."""
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ.update(
        {
            "DATABASE_URL": url,
            "REDIS_URL": "memory://",
            "EVALHARNESS_SCENARIOS_DIR": str(SCENARIOS_DIR),
            "EVALHARNESS_MODELS_FILE": str(MODELS_FILE),
            "EVALHARNESS_CELERY_ALWAYS_EAGER": "1",
            "EVALHARNESS_READ_ONLY": "true" if read_only else "false",
        }
    )

    from evalharness.api import deps
    from evalharness.config import get_settings

    get_settings.cache_clear()
    deps.reset_caches()
    try:
        yield url
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        deps.reset_caches()


@contextlib.asynccontextmanager
async def _client(url: str) -> AsyncIterator[AsyncClient]:
    from evalharness.api.app import create_app
    from evalharness.api.events import MemoryEventBus, set_event_bus
    from evalharness.db.session import create_database, set_database

    database = create_database(url)
    await database.create_all()
    set_database(database)
    set_event_bus(MemoryEventBus())

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    set_event_bus(None)
    set_database(None)
    await database.dispose()


@pytest_asyncio.fixture
async def read_only_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    with _configured(tmp_path / "readonly.sqlite", read_only=True) as url:
        async with _client(url) as http:
            yield http


# ------------------------------------------------------------------ writes refused


async def test_launching_a_run_is_refused(read_only_client: AsyncClient) -> None:
    response = await read_only_client.post("/api/runs", json=LAUNCH)
    assert response.status_code == 405, response.text
    assert "read-only" in response.json()["detail"]


async def test_cancelling_is_refused(read_only_client: AsyncClient) -> None:
    """The guard runs before the run lookup, so an unknown id still gets 405.

    That ordering matters: a 404 here would tell an anonymous caller whether a
    given run id exists on a deployment that has no business answering writes.
    """
    response = await read_only_client.post("/api/runs/does-not-exist/cancel")
    assert response.status_code == 405, response.text
    assert "read-only" in response.json()["detail"]


async def test_the_refusal_is_not_a_blanket_failure(read_only_client: AsyncClient) -> None:
    """Reads must keep working -- serving them is the entire point of the demo."""
    health = await read_only_client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    runs = await read_only_client.get("/api/runs")
    assert runs.status_code == 200

    leaderboard = await read_only_client.get("/api/leaderboard")
    assert leaderboard.status_code == 200


# ------------------------------------------------------------------ the negative half


async def test_the_writes_succeed_with_the_flag_off(tmp_path: Path) -> None:
    """Proof the 405s above come from the flag and not from a broken request.

    Same payload, same endpoints, flag off: the run is accepted and the cancel is
    answered on its merits. Without this, every assertion above would still pass
    if `POST /api/runs` were rejecting for some unrelated reason.
    """
    with _configured(tmp_path / "writable.sqlite", read_only=False) as url:
        async with _client(url) as http:
            created = await http.post("/api/runs", json=LAUNCH)
            assert created.status_code == 201, created.text
            run_id = created.json()["id"]

            cancelled = await http.post(f"/api/runs/{run_id}/cancel")
            assert cancelled.status_code != 405, cancelled.text
