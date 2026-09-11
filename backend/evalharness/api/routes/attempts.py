"""Attempt traces (SPEC 9.2).

One route, and it carries the product's most important page: the trace viewer
needs the turns, the full ordered event log and every assertion result in a
single response so the timeline can be rendered without follow-up requests.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from evalharness.api.deps import SessionDep, get_scenarios
from evalharness.api.routes.serializers import (
    assertion_to_api,
    attempt_to_api,
    event_to_api,
    run_to_api,
    turn_to_api,
)
from evalharness.api.schemas import AttemptTrace
from evalharness.db import repository

router = APIRouter(tags=["attempts"])


@router.get(
    "/attempts/{attempt_id}",
    response_model=AttemptTrace,
    operation_id="getAttempt",
    summary="Full trace: turns, ordered events and assertion results",
)
async def get_attempt(attempt_id: str, session: SessionDep) -> AttemptTrace:
    attempt = await repository.get_attempt(session, attempt_id)
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown attempt {attempt_id!r}")

    run = await repository.get_run(session, attempt.run_id)
    if run is None:  # pragma: no cover - foreign key makes this unreachable
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {attempt.run_id!r}")

    turns = await repository.list_turns(session, attempt_id)
    events = await repository.list_events(session, attempt_id)
    assertions = await repository.list_assertion_results(session, attempt_id)

    # Title comes from the scenario on disk; the config hash comes from the run,
    # because that is the definition this attempt actually executed against.
    entry = get_scenarios().get(attempt.scenario_id)
    title = ""
    if entry is not None and entry.loaded is not None:
        title = entry.loaded.scenario.title

    return AttemptTrace(
        attempt=attempt_to_api(attempt),
        run=run_to_api(run),
        scenario_id=attempt.scenario_id,
        scenario_title=title,
        config_hash=dict(run.config_hashes or {}).get(attempt.scenario_id, ""),
        turns=[turn_to_api(turn) for turn in turns],
        events=[event_to_api(event) for event in events],
        assertion_results=[assertion_to_api(row) for row in assertions],
    )
