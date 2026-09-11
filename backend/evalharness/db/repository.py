"""Data access for runs, attempts and the audit trail.

Everything the API and the worker do to the database goes through here, so the
append-only rule for the audit trail is enforceable in one place:

**``events`` and ``assertion_results`` have append-only writers and no update or
delete functions, deliberately (SPEC 8.3). Do not add any.** Rewriting a graded
result would destroy the evidence a run is supposed to preserve; a correction is
a new run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from evalharness.db import models
from evalharness.schema.enums import AttemptStatus, RunStatus
from evalharness.schema.runtime import AssertionResult, AttemptResult, Event, TurnRecord

#: Statuses a run or attempt can no longer move out of.
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)
TERMINAL_ATTEMPT_STATUSES: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.CANCELLED}
)


# --------------------------------------------------------------------------- runs


async def create_run(
    session: AsyncSession,
    *,
    model_key: str,
    litellm_model: str,
    params: dict[str, Any],
    k: int,
    scenario_ids: list[str],
    config_hashes: dict[str, str],
    git_commit: str,
    harness_version: str,
    scenario_snapshots: dict[str, Any],
) -> models.Run:
    """Create a queued run together with its queued attempts (SPEC 8.2)."""
    run = models.Run(
        status=RunStatus.QUEUED,
        model_key=model_key,
        litellm_model=litellm_model,
        params=params,
        k=k,
        scenario_ids=scenario_ids,
        config_hashes=config_hashes,
        git_commit=git_commit,
        harness_version=harness_version,
        scenario_snapshots=scenario_snapshots,
    )
    session.add(run)
    await session.flush()
    for scenario_id in scenario_ids:
        for repetition in range(1, k + 1):
            session.add(
                models.Attempt(
                    run_id=run.id,
                    scenario_id=scenario_id,
                    repetition=repetition,
                    status=AttemptStatus.QUEUED,
                )
            )
    await session.flush()
    return run


async def get_run(session: AsyncSession, run_id: str) -> models.Run | None:
    return await session.get(models.Run, run_id)


async def list_runs(
    session: AsyncSession,
    *,
    model_key: str | None = None,
    status: RunStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[models.Run], int]:
    """A page of runs, newest first, plus the total matching count."""
    statement = select(models.Run)
    count_statement = select(func.count()).select_from(models.Run)
    if model_key is not None:
        statement = statement.where(models.Run.model_key == model_key)
        count_statement = count_statement.where(models.Run.model_key == model_key)
    if status is not None:
        statement = statement.where(models.Run.status == status)
        count_statement = count_statement.where(models.Run.status == status)

    statement = statement.order_by(models.Run.created_at.desc(), models.Run.id.desc())
    statement = statement.limit(limit).offset(offset)
    rows = (await session.execute(statement)).scalars().all()
    total = int((await session.execute(count_statement)).scalar_one())
    return rows, total


async def set_run_status(
    session: AsyncSession,
    run_id: str,
    status: RunStatus,
    *,
    only_if: Iterable[RunStatus] | None = None,
) -> bool:
    """Move a run to ``status``. Returns False when ``only_if`` did not match."""
    statement = update(models.Run).where(models.Run.id == run_id)
    if only_if is not None:
        statement = statement.where(models.Run.status.in_(list(only_if)))
    result = await session.execute(statement.values(status=status))
    return bool(cast("CursorResult[Any]", result).rowcount)


async def claim_run_finalization(session: AsyncSession, run_id: str) -> bool:
    """Elect exactly one finalizer for a run.

    ``summary`` is written by whoever wins this conditional update, so parallel
    workers finishing their last attempts cannot both compute a summary.
    """
    result = await session.execute(
        update(models.Run)
        .where(models.Run.id == run_id, models.Run.summary.is_(None))
        .values(summary={"status": "computing"})
    )
    return bool(cast("CursorResult[Any]", result).rowcount)


async def set_run_summary(
    session: AsyncSession, run_id: str, summary: dict[str, Any], status: RunStatus
) -> None:
    await session.execute(
        update(models.Run).where(models.Run.id == run_id).values(summary=summary, status=status)
    )


# ------------------------------------------------------------------------ attempts


async def get_attempt(session: AsyncSession, attempt_id: str) -> models.Attempt | None:
    return await session.get(models.Attempt, attempt_id)


async def find_attempt(
    session: AsyncSession, run_id: str, scenario_id: str, repetition: int
) -> models.Attempt | None:
    statement = select(models.Attempt).where(
        models.Attempt.run_id == run_id,
        models.Attempt.scenario_id == scenario_id,
        models.Attempt.repetition == repetition,
    )
    return (await session.execute(statement)).scalars().first()


async def list_attempts(session: AsyncSession, run_id: str) -> Sequence[models.Attempt]:
    statement = (
        select(models.Attempt)
        .where(models.Attempt.run_id == run_id)
        .order_by(models.Attempt.scenario_id, models.Attempt.repetition)
    )
    return (await session.execute(statement)).scalars().all()


async def set_attempt_status(
    session: AsyncSession,
    attempt_id: str,
    status: AttemptStatus,
    *,
    error: str | None = None,
    only_if: Iterable[AttemptStatus] | None = None,
) -> bool:
    statement = update(models.Attempt).where(models.Attempt.id == attempt_id)
    if only_if is not None:
        statement = statement.where(models.Attempt.status.in_(list(only_if)))
    values: dict[str, Any] = {"status": status}
    if error is not None:
        values["error"] = error
    result = await session.execute(statement.values(**values))
    return bool(cast("CursorResult[Any]", result).rowcount)


async def cancel_pending_attempts(session: AsyncSession, run_id: str) -> int:
    """Mark every not-yet-terminal attempt of a run cancelled."""
    result = await session.execute(
        update(models.Attempt)
        .where(
            models.Attempt.run_id == run_id,
            models.Attempt.status.in_([AttemptStatus.QUEUED, AttemptStatus.RUNNING]),
        )
        .values(status=AttemptStatus.CANCELLED)
    )
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def attempt_status_counts(session: AsyncSession, run_id: str) -> dict[str, int]:
    statement = (
        select(models.Attempt.status, func.count())
        .where(models.Attempt.run_id == run_id)
        .group_by(models.Attempt.status)
    )
    rows = (await session.execute(statement)).all()
    return {str(status): int(count) for status, count in rows}


async def all_attempts_terminal(session: AsyncSession, run_id: str) -> bool:
    statement = select(func.count()).where(
        models.Attempt.run_id == run_id,
        models.Attempt.status.in_([AttemptStatus.QUEUED, AttemptStatus.RUNNING]),
    )
    return int((await session.execute(statement)).scalar_one()) == 0


async def record_attempt_outcome(
    session: AsyncSession,
    attempt_id: str,
    result: AttemptResult,
    *,
    status: AttemptStatus,
) -> None:
    """Write the scored fields of a finished attempt (SPEC 6.3, 8.3)."""
    await session.execute(
        update(models.Attempt)
        .where(models.Attempt.id == attempt_id)
        .values(
            status=status,
            passed=result.passed,
            critical_failure=result.critical_failure,
            axis_scores=dict(result.axis_scores),
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            error=result.error,
        )
    )


# ------------------------------------------------------- audit trail (append-only)


async def append_events(session: AsyncSession, attempt_id: str, events: Iterable[Event]) -> int:
    """Append audit events. There is no counterpart update or delete, by design."""
    rows = [
        models.Event(
            attempt_id=attempt_id,
            turn_index=event.turn_index,
            seq=event.seq,
            type=str(event.type),
            payload=dict(event.payload),
            latency_ms=event.latency_ms,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            **({"created_at": event.created_at} if event.created_at is not None else {}),
        )
        for event in events
    ]
    if rows:
        session.add_all(rows)
        await session.flush()
    return len(rows)


async def append_assertion_results(
    session: AsyncSession, attempt_id: str, results: Iterable[AssertionResult]
) -> int:
    """Append graded assertions. Append-only, like ``events``."""
    rows = [
        models.AssertionResult(
            attempt_id=attempt_id,
            turn_index=result.turn_index,
            assertion_id=result.assertion_id,
            type=result.type,
            axis=str(result.axis),
            severity=str(result.severity),
            passed=result.passed,
            reason=result.reason,
            details=dict(result.details),
            non_deterministic=result.non_deterministic,
        )
        for result in results
    ]
    if rows:
        session.add_all(rows)
        await session.flush()
    return len(rows)


async def insert_turns(session: AsyncSession, attempt_id: str, turns: Iterable[TurnRecord]) -> int:
    rows = [
        models.Turn(
            attempt_id=attempt_id,
            index=turn.index,
            user_message=turn.user_message,
            final_response=turn.final_response,
            passed=turn.passed,
            limit_exceeded=turn.limit_exceeded,
        )
        for turn in turns
    ]
    if rows:
        session.add_all(rows)
        await session.flush()
    return len(rows)


# ----------------------------------------------------------------- trace readers


async def list_events(session: AsyncSession, attempt_id: str) -> Sequence[models.Event]:
    statement = (
        select(models.Event)
        .where(models.Event.attempt_id == attempt_id)
        .order_by(models.Event.seq, models.Event.id)
    )
    return (await session.execute(statement)).scalars().all()


async def list_turns(session: AsyncSession, attempt_id: str) -> Sequence[models.Turn]:
    statement = (
        select(models.Turn).where(models.Turn.attempt_id == attempt_id).order_by(models.Turn.index)
    )
    return (await session.execute(statement)).scalars().all()


async def list_assertion_results(
    session: AsyncSession, attempt_id: str
) -> Sequence[models.AssertionResult]:
    statement = (
        select(models.AssertionResult)
        .where(models.AssertionResult.attempt_id == attempt_id)
        .order_by(models.AssertionResult.turn_index, models.AssertionResult.id)
    )
    return (await session.execute(statement)).scalars().all()


async def run_assertion_rows(
    session: AsyncSession, run_id: str
) -> Sequence[tuple[str, str, str, str, str, bool]]:
    """``(scenario_id, assertion_id, type, axis, severity, passed)`` for a whole run.

    This feeds the compare view's "which assertions flipped" section, which needs
    every graded assertion of both runs but none of their payloads.
    """
    statement = (
        select(
            models.Attempt.scenario_id,
            models.AssertionResult.assertion_id,
            models.AssertionResult.type,
            models.AssertionResult.axis,
            models.AssertionResult.severity,
            models.AssertionResult.passed,
        )
        .join(models.Attempt, models.Attempt.id == models.AssertionResult.attempt_id)
        .where(models.Attempt.run_id == run_id)
        .order_by(models.Attempt.scenario_id, models.AssertionResult.assertion_id)
    )
    return [
        (str(a), str(b), str(c), str(d), str(e), bool(f))
        for a, b, c, d, e, f in (await session.execute(statement)).all()
    ]


# ------------------------------------------------------------- summary ingredients


async def model_call_latencies(session: AsyncSession, run_id: str) -> list[tuple[str, int]]:
    """``(scenario_id, latency_ms)`` for every model call, from ``model_response`` events."""
    statement = (
        select(models.Attempt.scenario_id, models.Event.latency_ms)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(
            models.Attempt.run_id == run_id,
            models.Event.type == "model_response",
            models.Event.latency_ms.is_not(None),
        )
    )
    return [(str(scenario), int(value)) for scenario, value in (await session.execute(statement))]


async def model_call_waits(session: AsyncSession, run_id: str) -> list[int]:
    """Milliseconds each model call spent queued or backing off (D28).

    Reported separately from latency so that p50/p95 describe how fast the model
    answered, not how long the harness throttled itself in front of it.
    """
    statement = (
        select(models.Event.payload)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(models.Attempt.run_id == run_id, models.Event.type == "model_response")
    )
    waits: list[int] = []
    for (payload,) in (await session.execute(statement)).all():
        value = (payload or {}).get("wait_ms")
        if isinstance(value, int | float):
            waits.append(int(value))
    return waits


async def request_shaping(session: AsyncSession, run_id: str) -> tuple[int, int]:
    """``(http_requests, limiter_acquires)`` for a whole run (D32).

    The invariant is that these are equal: one slot per HTTP request. Any gap is
    requests that went out unshaped, which is what let a run make 268 requests
    against 108 slots and trip a provider cap it was supposedly under.
    """
    statement = (
        select(models.Event.payload)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(models.Attempt.run_id == run_id, models.Event.type == "model_response")
    )
    requests = acquires = 0
    for (payload,) in (await session.execute(statement)).all():
        data = payload or {}
        value = data.get("http_requests")
        requests += int(value) if isinstance(value, int | float) else 0
        value = data.get("rate_limit_acquires")
        acquires += int(value) if isinstance(value, int | float) else 0
    return requests, acquires


async def rate_limit_bypasses(session: AsyncSession, run_id: str) -> int:
    """Model calls that skipped the throttle (D31)."""
    statement = (
        select(models.Event.payload)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(models.Attempt.run_id == run_id, models.Event.type == "model_response")
    )
    return sum(
        1
        for (payload,) in (await session.execute(statement)).all()
        if (payload or {}).get("rate_limit_bypassed") is True
    )


async def truncated_attempts(session: AsyncSession, run_id: str) -> int:
    """Attempts with at least one response cut off by max_tokens (D35).

    Counted from the event trail rather than a column on ``attempts``: the
    events are append-only and already record every truncation with the step it
    happened on, so a derived count cannot drift from the evidence for it.
    """
    statement = (
        select(func.count(func.distinct(models.Event.attempt_id)))
        .select_from(models.Event)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(models.Attempt.run_id == run_id, models.Event.type == "truncated")
    )
    return int((await session.execute(statement)).scalar_one() or 0)


async def steps_per_turn(session: AsyncSession, run_id: str) -> list[tuple[str, int]]:
    """``(scenario_id, model calls)`` per turn, for the mean-steps-per-turn stat."""
    statement = (
        select(models.Attempt.scenario_id, func.count())
        .select_from(models.Event)
        .join(models.Attempt, models.Attempt.id == models.Event.attempt_id)
        .where(
            models.Attempt.run_id == run_id,
            models.Event.type == "model_response",
            models.Event.turn_index.is_not(None),
        )
        .group_by(models.Attempt.scenario_id, models.Event.attempt_id, models.Event.turn_index)
    )
    return [(str(scenario), int(count)) for scenario, count in (await session.execute(statement))]
