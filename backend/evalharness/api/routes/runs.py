"""Run lifecycle: create, list, inspect, stream and cancel (SPEC 9.2)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from evalharness import __version__
from evalharness.api import schemas
from evalharness.api.deps import SessionDep, get_registry, get_scenarios, settings
from evalharness.api.events import EventBus, get_event_bus
from evalharness.api.routes.serializers import run_detail, run_to_api
from evalharness.db import repository
from evalharness.db.session import Database, get_database
from evalharness.schema.enums import RunStatus

LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])

#: A stream that has seen nothing for this long still emits a heartbeat, so
#: proxies and browsers keep the connection open.
HEARTBEAT_AFTER_IDLE_TICKS = 15
#: Idle ticks between database polls for the run's terminal status. The stream
#: never depends on pub/sub alone: a dropped message must not hang the UI.
DB_POLL_EVERY_IDLE_TICKS = 2


@router.post(
    "/runs",
    response_model=schemas.RunDetail,
    status_code=status.HTTP_201_CREATED,
    operation_id="createRun",
    summary="Create a run and queue it for execution",
)
async def create_run(body: schemas.RunCreate, session: SessionDep) -> schemas.RunDetail:
    registry = get_registry()
    try:
        entry = registry.get(body.model_key)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if not entry.supports_tool_calling:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"model {entry.key!r} has no native tool calling and cannot be evaluated",
        )

    scenarios = get_scenarios()
    requested = list(dict.fromkeys(body.scenario_ids))
    missing = [sid for sid in requested if sid not in scenarios]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown scenario(s): {', '.join(sorted(missing))}"
        )
    invalid = [sid for sid in requested if scenarios[sid].loaded is None]
    if invalid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"scenario(s) failed validation: {', '.join(sorted(invalid))}",
        )

    # SPEC 8.2: the run records exactly which definition produced it.
    config_hashes: dict[str, str] = {}
    snapshots: dict[str, Any] = {}
    for scenario_id in requested:
        loaded = scenarios[scenario_id].loaded
        assert loaded is not None
        config_hashes[scenario_id] = loaded.config_hash
        snapshots[scenario_id] = loaded.snapshot()

    # effective_params folds in reasoning effort and provider pinning, so the
    # stored record matches the request the provider actually receives (D20).
    params: dict[str, Any] = entry.effective_params(body.params_override)
    if body.max_cost_usd is not None:
        # Rides in params like `transcript` does; the worker reads it per attempt.
        params["max_cost_usd"] = body.max_cost_usd
    if body.transcript is not None:
        # FakeModel only; the worker pops this before params reach a provider.
        params["transcript"] = body.transcript

    run = await repository.create_run(
        session,
        model_key=entry.key,
        litellm_model=entry.litellm_model,
        params=params,
        k=body.k,
        scenario_ids=requested,
        config_hashes=config_hashes,
        git_commit=_git_commit(),
        harness_version=__version__,
        scenario_snapshots=snapshots,
    )
    await session.commit()

    attempts = list(await repository.list_attempts(session, run.id))
    _queue_run(run.id)
    return run_detail(run, attempts, await repository.attempt_status_counts(session, run.id))


def _git_commit() -> str:
    from evalharness.loader import git_commit

    return git_commit(Path_of_repo_root())


def Path_of_repo_root() -> Any:  # noqa: N802 - tiny helper, kept next to its caller
    """The repo root, inferred from the configured scenarios directory."""
    from pathlib import Path

    return Path(str(settings().scenarios_dir)).parent


def _queue_run(run_id: str) -> None:
    """Hand the run to Celery. A broker outage is a 503, not a silent no-op."""
    from evalharness.worker.tasks import execute_run

    try:
        execute_run.delay(run_id)
    except Exception as exc:
        LOGGER.exception("could not queue run %s", run_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"run {run_id} was created but could not be queued: {exc}",
        ) from exc


@router.get(
    "/runs",
    response_model=schemas.RunListResponse,
    operation_id="listRuns",
    summary="Paginated runs, filterable by model and status",
)
async def list_runs(
    session: SessionDep,
    model_key: Annotated[str | None, Query()] = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> schemas.RunListResponse:
    rows, total = await repository.list_runs(
        session, model_key=model_key, status=run_status, limit=limit, offset=offset
    )
    items = []
    for run in rows:
        counts = await repository.attempt_status_counts(session, run.id)
        items.append(run_to_api(run, counts))
    return schemas.RunListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/runs/{run_id}",
    response_model=schemas.RunDetail,
    operation_id="getRun",
    summary="One run with its attempts and summary",
)
async def get_run(run_id: str, session: SessionDep) -> schemas.RunDetail:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id!r}")
    attempts = list(await repository.list_attempts(session, run_id))
    counts = await repository.attempt_status_counts(session, run_id)
    return run_detail(run, attempts, counts)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=schemas.CancelResponse,
    operation_id="cancelRun",
    summary="Cancel a queued or running run",
)
async def cancel_run(run_id: str, session: SessionDep) -> schemas.CancelResponse:
    """Set the cancellation flag that attempts check between model calls.

    The flag lives in two places on purpose: Redis, which a running attempt polls
    cheaply, and the run's status in Postgres, which is authoritative and
    survives a Redis restart.
    """
    from evalharness.worker.concurrency import get_cancel_store

    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id!r}")

    if run.status in repository.TERMINAL_RUN_STATUSES:
        attempts = list(await repository.list_attempts(session, run_id))
        counts = await repository.attempt_status_counts(session, run_id)
        return schemas.CancelResponse(
            run=run_detail(run, attempts, counts), cancelled_attempts=0, already_terminal=True
        )

    await get_cancel_store().mark_cancelled(run_id)
    await repository.set_run_status(session, run_id, RunStatus.CANCELLED)
    cancelled = await repository.cancel_pending_attempts(session, run_id)
    await session.commit()
    await session.refresh(run)

    attempts = list(await repository.list_attempts(session, run_id))
    counts = await repository.attempt_status_counts(session, run_id)
    await get_event_bus().publish(
        schemas.ProgressEvent(
            type="run_completed",
            run_id=run_id,
            run_status=RunStatus.CANCELLED,
            attempt_counts=counts,
            total_attempts=len(attempts),
            completed_attempts=len(attempts),
            message="cancelled",
            ts=datetime.now(UTC),
        )
    )
    _queue_finalize(run_id)
    return schemas.CancelResponse(
        run=run_detail(run, attempts, counts),
        cancelled_attempts=cancelled,
        already_terminal=False,
    )


def _queue_finalize(run_id: str) -> None:
    """Ask for a summary now; the task no-ops while attempts are still running."""
    from evalharness.worker.tasks import finalize_run

    try:
        finalize_run.delay(run_id)
    except Exception as exc:
        LOGGER.warning("could not queue finalize for run %s: %s", run_id, exc)


# ----------------------------------------------------------------------- SSE


@router.get(
    "/runs/{run_id}/stream",
    operation_id="streamRun",
    summary="Server-Sent Events: attempt started/finished, run completed",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": "A text/event-stream whose data frames are ProgressEvent JSON.",
            # `model` (rather than a hand-written $ref) is what makes FastAPI emit
            # ProgressEvent into components/schemas; a bare $ref to a schema no
            # route returns dangles, and openapi-typescript refuses to resolve it.
            "model": schemas.ProgressEvent,
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_run(run_id: str, request: Request, session: SessionDep) -> EventSourceResponse:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id!r}")

    async def publisher() -> AsyncIterator[dict[str, str]]:
        async for event in stream_run_progress(run_id, request=request):
            yield {"event": event.type, "data": event.model_dump_json()}

    return EventSourceResponse(publisher())


async def snapshot_event(
    database: Database, run_id: str, event_type: schemas.ProgressEventType = "snapshot"
) -> schemas.ProgressEvent:
    async with database.session() as session:
        run = await repository.get_run(session, run_id)
        counts = await repository.attempt_status_counts(session, run_id)
    total = sum(counts.values())
    done = sum(counts.get(name, 0) for name in ("completed", "failed", "cancelled"))
    return schemas.ProgressEvent(
        type=event_type,
        run_id=run_id,
        run_status=run.status if run is not None else None,
        attempt_counts=counts,
        completed_attempts=done,
        total_attempts=total,
        ts=datetime.now(UTC),
    )


async def stream_run_progress(
    run_id: str,
    *,
    request: Request | None = None,
    database: Database | None = None,
    bus: EventBus | None = None,
) -> AsyncIterator[schemas.ProgressEvent]:
    """Snapshot, then live progress, ending with ``run_completed``.

    The snapshot comes first so a client that connects late (or reconnects) sees
    the current state without waiting for the next event.
    """
    database = database or get_database()
    bus = bus or get_event_bus()

    backlog = bus.history(run_id)
    snapshot = await snapshot_event(database, run_id)
    yield snapshot
    for event in backlog:
        yield event

    if any(event.type == "run_completed" for event in backlog):
        return
    if snapshot.run_status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
        yield await snapshot_event(database, run_id, "run_completed")
        return

    idle_ticks = 0
    async for message in bus.subscribe(run_id, skip=len(backlog)):
        if request is not None and await request.is_disconnected():
            return
        if message is not None:
            idle_ticks = 0
            yield message
            if message.type == "run_completed":
                return
            continue

        idle_ticks += 1
        if idle_ticks % DB_POLL_EVERY_IDLE_TICKS == 0:
            current = await snapshot_event(database, run_id)
            if current.run_status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                yield await snapshot_event(database, run_id, "run_completed")
                return
        if idle_ticks % HEARTBEAT_AFTER_IDLE_TICKS == 0:
            yield await snapshot_event(database, run_id, "heartbeat")
