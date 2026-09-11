"""Celery tasks: fan out a run, execute attempts, finalize (SPEC 9.1).

Shape of the pipeline::

    execute_run(run_id)
      -> execute_attempt(run_id, scenario_id, repetition)   [one per attempt]
           -> finalize_run(run_id)                          [whoever finishes last]

Notes that matter when reading this:

* Celery tasks are sync entry points, so each one runs its async body with
  ``run_async``. Every task builds and disposes its own ``Database`` inside that
  loop -- an async engine may never be shared across event loops.
* ``finalize_run`` is dispatched by the last attempt to reach a terminal state
  rather than by a chord. A chord would be tidier on paper, but this survives a
  worker dying mid-fan-out, and the race is settled by a conditional UPDATE
  (``claim_run_finalization``) so exactly one finalizer wins.
* The runner is imported lazily inside the task body: the API process imports
  this module, and must not pay for (or be broken by) the model stack.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from evalharness.api.events import get_event_bus, now
from evalharness.api.schemas import (
    ProgressEvent,
    ProgressEventType,
    RunSummary,
    ScenarioStats,
)
from evalharness.db import Database, database_scope, models, repository
from evalharness.schema.enums import AttemptStatus, RunStatus
from evalharness.schema.registry import ModelEntry
from evalharness.schema.runtime import AttemptResult, Event
from evalharness.worker.app import app
from evalharness.worker.concurrency import (
    default_concurrency,
    get_cancel_store,
    provider_of,
    provider_slot,
)

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

#: Events are flushed to Postgres in batches so a long attempt's trace is
#: visible while it runs, without a round trip per event.
EVENT_FLUSH_BATCH = 25
#: How often a running attempt re-checks the database for a cancelled run.
CANCEL_DB_POLL_SECONDS = 2.0


class TaskError(RuntimeError):
    """A task could not proceed; the attempt is recorded as failed."""


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async body from a sync Celery task.

    Under ``task_always_eager`` (tests, and the API process itself) a loop may
    already be running in this thread, and ``asyncio.run`` refuses to nest -- so
    the work moves to a short-lived thread with a loop of its own.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="evalharness-task") as pool:
        return pool.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------- settings access


def scenarios_dir() -> Path:
    from evalharness.config import get_settings

    return Path(get_settings().scenarios_dir)


def models_file() -> Path:
    from evalharness.config import get_settings

    return Path(get_settings().models_file)


def load_scenario_for_run(scenario_id: str, expected_hash: str | None = None) -> Any:
    """Load a scenario from disk for execution, warning if it drifted mid-run."""
    from evalharness.loader import load_scenario

    loaded = load_scenario(scenarios_dir() / scenario_id)
    if expected_hash and loaded.config_hash != expected_hash:
        LOGGER.warning(
            "scenario %s changed since the run was created (%s -> %s); "
            "the run keeps the hash it was created with",
            scenario_id,
            expected_hash,
            loaded.config_hash,
        )
    return loaded


def model_entry(model_key: str) -> ModelEntry:
    from evalharness.loader import load_registry

    return load_registry(models_file()).get(model_key)


def is_fake_model(entry: ModelEntry) -> bool:
    return entry.litellm_model.split("/", 1)[0] == "fake"


def build_provider(
    entry: ModelEntry,
    loaded: Any,
    transcript_name: str | None,
) -> Any:
    """Pick the provider adapter for this run (SPEC 6.4)."""
    from evalharness import runner as runner_module

    if is_fake_model(entry):
        from evalharness.loader import load_transcripts

        transcripts = load_transcripts(loaded.directory)
        name = transcript_name or "golden"
        if name not in transcripts:
            available = ", ".join(sorted(transcripts)) or "none"
            raise TaskError(
                f"scenario {loaded.id!r} has no transcript {name!r} (available: {available})"
            )
        return runner_module.FakeModel(transcripts[name])
    return runner_module.LiteLLMProvider(entry)


async def execute_one_attempt(
    loaded: Any,
    provider: Any,
    repetition: int,
    cancel_check: Callable[[], Coroutine[Any, Any, bool]] | None,
    on_event: Callable[[Event], Coroutine[Any, Any, None]] | None,
    params: dict[str, Any] | None = None,
    rate_limit: Callable[[], Coroutine[Any, Any, tuple[float, bool]]] | None = None,
) -> AttemptResult:
    """Thin seam over the runner: imported lazily, and monkeypatched in tests."""
    from evalharness.runner.conversation import AttemptContext, run_attempt

    context = AttemptContext(
        loaded=loaded,
        provider=provider,
        repetition=repetition,
        cancel_check=cancel_check,
        on_event=on_event,
        params=dict(params or {}),
        rate_limit=rate_limit,
    )
    return await run_attempt(context)


# ------------------------------------------------------------------- cancellation


class CancellationWatcher:
    """``AttemptContext.cancel_check``: cheap Redis flag, database as the backstop."""

    def __init__(
        self,
        run_id: str,
        database: Database,
        *,
        db_poll_s: float = CANCEL_DB_POLL_SECONDS,
    ):
        self.run_id = run_id
        self._database = database
        self._db_poll_s = db_poll_s
        self._cancelled = False
        self._last_db_check = 0.0

    async def __call__(self) -> bool:
        if self._cancelled:
            return True
        if await get_cancel_store().is_cancelled(self.run_id):
            self._cancelled = True
            return True
        elapsed = time.monotonic() - self._last_db_check
        if elapsed >= self._db_poll_s:
            self._last_db_check = time.monotonic()
            async with self._database.session() as session:
                run = await repository.get_run(session, self.run_id)
            if run is not None and run.status == RunStatus.CANCELLED:
                self._cancelled = True
        return self._cancelled


# ---------------------------------------------------------------------- progress


async def publish(event: ProgressEvent) -> None:
    await get_event_bus().publish(event)


async def progress_for_run(
    database: Database,
    run_id: str,
    event_type: ProgressEventType,
    **fields: Any,
) -> ProgressEvent:
    async with database.session() as session:
        counts = await repository.attempt_status_counts(session, run_id)
        run = await repository.get_run(session, run_id)
    explicit_status = fields.pop("run_status", None)
    total = sum(counts.values())
    completed = sum(
        counts.get(str(status), 0)
        for status in (
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.ERRORED,
            AttemptStatus.CANCELLED,
        )
    )
    return ProgressEvent(
        type=event_type,
        run_id=run_id,
        run_status=explicit_status or (run.status if run is not None else None),
        attempt_counts=counts,
        completed_attempts=completed,
        total_attempts=total,
        ts=now(),
        **fields,
    )


# --------------------------------------------------------------------- execute_run


@app.task(name="evalharness.execute_run", bind=True, max_retries=3)
def execute_run(self: Any, run_id: str) -> dict[str, Any]:
    """Fan a run out into one ``execute_attempt`` task per attempt."""
    return run_async(_execute_run(run_id))


async def _execute_run(run_id: str) -> dict[str, Any]:
    async with database_scope() as database:
        async with database.session() as session:
            run = await repository.get_run(session, run_id)
            if run is None:
                raise TaskError(f"run {run_id} does not exist")
            if run.status == RunStatus.CANCELLED:
                LOGGER.info("run %s was cancelled before it started", run_id)
                return {"run_id": run_id, "dispatched": 0, "cancelled": True}
            await repository.set_run_status(
                session, run_id, RunStatus.RUNNING, only_if=[RunStatus.QUEUED]
            )
            attempts = await repository.list_attempts(session, run_id)
            targets = [
                (attempt.scenario_id, attempt.repetition)
                for attempt in attempts
                if attempt.status == AttemptStatus.QUEUED
            ]

        await publish(await progress_for_run(database, run_id, "run_started"))

    for scenario_id, repetition in targets:
        execute_attempt.delay(run_id, scenario_id, repetition)

    if not targets:
        finalize_run.delay(run_id)
    return {"run_id": run_id, "dispatched": len(targets)}


# ----------------------------------------------------------------- execute_attempt


@app.task(name="evalharness.execute_attempt", bind=True)
def execute_attempt(self: Any, run_id: str, scenario_id: str, repetition: int) -> dict[str, Any]:
    """Execute one scenario x repetition, persisting its full trace."""
    return run_async(_execute_attempt(run_id, scenario_id, repetition))


def _budget_for(run: Any) -> float | None:
    """The run's spend ceiling, if one was set at creation."""
    raw = (run.params or {}).get("max_cost_usd")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):  # pragma: no cover - params are validated on entry
        return None
    return value if value > 0 else None


async def _run_spend(database: Database, run_id: str) -> float:
    """USD already spent by this run's finished attempts.

    Attempts with unknown cost contribute 0 rather than blocking the guard: a
    ceiling that stops working the moment pricing is unavailable would be worse
    than one that under-counts.
    """
    async with database.session() as session:
        attempts = await repository.list_attempts(session, run_id)
    return sum(a.cost_usd for a in attempts if a.cost_usd is not None)


async def _execute_attempt(run_id: str, scenario_id: str, repetition: int) -> dict[str, Any]:
    async with database_scope() as database:
        async with database.session() as session:
            run = await repository.get_run(session, run_id)
            attempt = await repository.find_attempt(session, run_id, scenario_id, repetition)
        if run is None or attempt is None:
            raise TaskError(f"no attempt {scenario_id}#{repetition} on run {run_id}")
        attempt_id = attempt.id

        if attempt.status in repository.TERMINAL_ATTEMPT_STATUSES:
            return {"attempt_id": attempt_id, "status": str(attempt.status), "skipped": True}

        watcher = CancellationWatcher(run_id, database)
        if await watcher() or run.status == RunStatus.CANCELLED:
            await _finish_attempt(
                database, run_id, attempt_id, scenario_id, repetition, AttemptStatus.CANCELLED
            )
            await _dispatch_finalize_if_done(database, run_id)
            return {"attempt_id": attempt_id, "status": "cancelled"}

        # Spend ceiling (D21). Checked before starting, against what the run's
        # finished attempts have already cost, so the overshoot is bounded by the
        # attempts already in flight rather than by the whole remaining fan-out.
        budget = _budget_for(run)
        if budget is not None:
            spent = await _run_spend(database, run_id)
            if spent > budget:
                message = (
                    f"budget stop: run has spent ${spent:.4f} of its "
                    f"${budget:.2f} max_cost_usd limit"
                )
                LOGGER.warning("%s; skipping %s#%s", message, scenario_id, repetition)
                await _finish_attempt(
                    database,
                    run_id,
                    attempt_id,
                    scenario_id,
                    repetition,
                    AttemptStatus.ERRORED,
                    error=message,
                )
                await _dispatch_finalize_if_done(database, run_id)
                return {"attempt_id": attempt_id, "status": "errored", "reason": "budget"}

        async with database.session() as session:
            await repository.set_attempt_status(
                session,
                attempt_id,
                AttemptStatus.RUNNING,
                only_if=[AttemptStatus.QUEUED],
            )
        await publish(
            await progress_for_run(
                database,
                run_id,
                "attempt_started",
                attempt_id=attempt_id,
                scenario_id=scenario_id,
                repetition=repetition,
                attempt_status=AttemptStatus.RUNNING,
            )
        )

        provider_name = provider_of(run.litellm_model)
        status = AttemptStatus.COMPLETED
        result: AttemptResult | None = None
        error: str | None = None
        try:
            # One slot per attempt, held for its whole conversation: the point is
            # to cap how many conversations hammer one provider at once.
            async with provider_slot(provider_name, limit=default_concurrency()):
                result = await _run_and_persist(
                    database, run, attempt_id, scenario_id, repetition, watcher
                )
        except Exception as exc:
            LOGGER.exception("attempt %s failed", attempt_id)
            error = f"{type(exc).__name__}: {exc}"
            status = AttemptStatus.FAILED
        else:
            if await watcher():
                status = AttemptStatus.CANCELLED
            elif result.errored or result.error:
                status = AttemptStatus.ERRORED

        if result is None:
            result = AttemptResult(
                scenario_id=scenario_id, repetition=repetition, passed=False, error=error
            )
        async with database.session() as session:
            await repository.record_attempt_outcome(session, attempt_id, result, status=status)

        await publish(
            await progress_for_run(
                database,
                run_id,
                "attempt_finished",
                attempt_id=attempt_id,
                scenario_id=scenario_id,
                repetition=repetition,
                attempt_status=status,
                passed=result.passed,
                message=result.error,
            )
        )
        await _dispatch_finalize_if_done(database, run_id)
        return {"attempt_id": attempt_id, "status": str(status), "passed": result.passed}


async def _run_and_persist(
    database: Database,
    run: models.Run,
    attempt_id: str,
    scenario_id: str,
    repetition: int,
    watcher: CancellationWatcher,
) -> AttemptResult:
    """Run the conversation, streaming its events into Postgres as they happen."""
    loaded = load_scenario_for_run(scenario_id, run.config_hashes.get(scenario_id))
    entry = model_entry(run.model_key)
    params = dict(run.params or {})
    # Harness-level knobs ride in `params` but must never reach the provider:
    # everything left here is spread into the completion request.
    transcript = params.pop("transcript", None)
    params.pop("max_cost_usd", None)
    provider = build_provider(entry, loaded, transcript if isinstance(transcript, str) else None)

    buffer: list[Event] = []
    flushed_seqs: set[int] = set()

    async def flush() -> None:
        if not buffer:
            return
        batch, buffer[:] = list(buffer), []
        async with database.session() as session:
            await repository.append_events(session, attempt_id, batch)
        flushed_seqs.update(event.seq for event in batch)

    async def on_event(event: Event) -> None:
        buffer.append(event)
        if len(buffer) >= EVENT_FLUSH_BATCH:
            await flush()

    try:
        result = await execute_one_attempt(loaded, provider, repetition, watcher, on_event, params)
    finally:
        await flush()

    # Whatever the runner did or did not stream, the result is the source of
    # truth for the trace; append anything not already written (append-only).
    missing = [event for event in result.events if event.seq not in flushed_seqs]
    async with database.session() as session:
        await repository.insert_turns(session, attempt_id, result.turns)
        await repository.append_events(session, attempt_id, missing)
        await repository.append_assertion_results(session, attempt_id, result.assertion_results)
    return result


async def _finish_attempt(
    database: Database,
    run_id: str,
    attempt_id: str,
    scenario_id: str,
    repetition: int,
    status: AttemptStatus,
    *,
    error: str | None = None,
) -> None:
    async with database.session() as session:
        await repository.set_attempt_status(session, attempt_id, status, error=error)
    await publish(
        await progress_for_run(
            database,
            run_id,
            "attempt_finished",
            attempt_id=attempt_id,
            scenario_id=scenario_id,
            repetition=repetition,
            attempt_status=status,
            message=error,
        )
    )


async def _dispatch_finalize_if_done(database: Database, run_id: str) -> None:
    async with database.session() as session:
        done = await repository.all_attempts_terminal(session, run_id)
    if done:
        finalize_run.delay(run_id)


# -------------------------------------------------------------------- finalize_run


@app.task(name="evalharness.finalize_run", bind=True)
def finalize_run(self: Any, run_id: str) -> dict[str, Any]:
    """Compute and store ``runs.summary`` once every attempt is terminal."""
    return run_async(_finalize_run(run_id))


async def _finalize_run(run_id: str) -> dict[str, Any]:
    async with database_scope() as database:
        async with database.session() as session:
            if not await repository.all_attempts_terminal(session, run_id):
                return {"run_id": run_id, "finalized": False, "reason": "attempts still running"}
            if not await repository.claim_run_finalization(session, run_id):
                return {"run_id": run_id, "finalized": False, "reason": "already finalized"}

        async with database.session() as session:
            run = await repository.get_run(session, run_id)
            if run is None:
                raise TaskError(f"run {run_id} does not exist")
            attempts = list(await repository.list_attempts(session, run_id))
            latencies = await repository.model_call_latencies(session, run_id)
            steps = await repository.steps_per_turn(session, run_id)
            waits = await repository.model_call_waits(session, run_id)
            bypasses = await repository.rate_limit_bypasses(session, run_id)
            was_cancelled = run.status == RunStatus.CANCELLED
            summary = build_summary(
                k=run.k,
                config_hashes=dict(run.config_hashes or {}),
                attempts=attempts,
                latencies=latencies,
                steps=steps,
                waits=waits,
                bypasses=bypasses,
            )
            status = _final_status(attempts, was_cancelled=was_cancelled)
            await repository.set_run_summary(
                session, run_id, summary.model_dump(mode="json"), status
            )

        await publish(
            await progress_for_run(
                database, run_id, "run_completed", run_status=status, message=str(status)
            )
        )
        return {"run_id": run_id, "finalized": True, "status": str(status)}


def _final_status(attempts: Sequence[models.Attempt], *, was_cancelled: bool) -> RunStatus:
    statuses = [attempt.status for attempt in attempts]
    if was_cancelled or AttemptStatus.CANCELLED in statuses:
        return RunStatus.CANCELLED
    if statuses and all(
        status in (AttemptStatus.FAILED, AttemptStatus.ERRORED) for status in statuses
    ):
        return RunStatus.FAILED
    return RunStatus.COMPLETED


# ------------------------------------------------------------------------ scoring


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile; ``q`` in [0, 1]. None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _axis_means(attempts: Sequence[models.Attempt]) -> dict[str, float]:
    totals: dict[str, list[float]] = {}
    for attempt in attempts:
        for axis, score in (attempt.axis_scores or {}).items():
            totals.setdefault(axis, []).append(float(score))
    return {axis: sum(scores) / len(scores) for axis, scores in sorted(totals.items())}


def _cost(attempts: Sequence[models.Attempt]) -> float | None:
    """Sum of attempt costs, or None if any is unknown -- never a guess (SPEC 6.3)."""
    if not attempts or any(attempt.cost_usd is None for attempt in attempts):
        return None
    return sum(float(attempt.cost_usd or 0.0) for attempt in attempts)


def build_summary(
    *,
    k: int,
    config_hashes: dict[str, str],
    attempts: Sequence[models.Attempt],
    latencies: Sequence[tuple[str, int]],
    steps: Sequence[tuple[str, int]],
    waits: Sequence[int] = (),
    bypasses: int = 0,
) -> RunSummary:
    """Run-level scoring (SPEC 6.3).

    pass@1 is the mean attempt pass rate over attempts that actually completed;
    pass^k is the fraction of *scenarios* whose every repetition passed, which is
    the consistency number the harness exists to report.
    """
    completed = [a for a in attempts if a.status.is_graded]
    passed = [a for a in completed if a.passed]
    scenario_ids = sorted({a.scenario_id for a in attempts})

    per_scenario: list[ScenarioStats] = []
    scenarios_all_passed = 0
    scenarios_scored = 0
    for scenario_id in scenario_ids:
        scenario_attempts = [a for a in attempts if a.scenario_id == scenario_id]
        scenario_completed = [a for a in scenario_attempts if a.status.is_graded]
        scenario_passed = [a for a in scenario_completed if a.passed]
        # pass^k is only answerable when every repetition ran; a scenario with a
        # missing attempt leaves both sides of the ratio rather than scoring 0.
        scenario_complete = len(scenario_completed) == len(scenario_attempts) >= k
        all_passed = scenario_complete and len(scenario_passed) == len(scenario_completed)
        scenarios_scored += int(scenario_complete)
        scenarios_all_passed += int(all_passed)
        scenario_latencies = [float(ms) for sid, ms in latencies if sid == scenario_id]
        scenario_steps = [float(count) for sid, count in steps if sid == scenario_id]
        per_scenario.append(
            ScenarioStats(
                scenario_id=scenario_id,
                config_hash=config_hashes.get(scenario_id, ""),
                attempts=len(scenario_attempts),
                completed=len(scenario_completed),
                errored=len(scenario_attempts) - len(scenario_completed),
                coverage=(
                    len(scenario_completed) / len(scenario_attempts) if scenario_attempts else 0.0
                ),
                complete=scenario_complete,
                passed=len(scenario_passed),
                pass_at_1=(
                    len(scenario_passed) / len(scenario_completed) if scenario_completed else 0.0
                ),
                pass_hat_k=1.0 if all_passed else 0.0,
                axis_scores=_axis_means(scenario_completed),
                cost_usd=_cost(scenario_completed),
                latency_p50_ms=percentile(scenario_latencies, 0.5),
                latency_p95_ms=percentile(scenario_latencies, 0.95),
                input_tokens=sum(a.input_tokens for a in scenario_attempts),
                output_tokens=sum(a.output_tokens for a in scenario_attempts),
                mean_steps_per_turn=mean(scenario_steps),
            )
        )

    all_latencies = [float(ms) for _, ms in latencies]
    return RunSummary(
        k=k,
        total_attempts=len(attempts),
        completed_attempts=len(completed),
        coverage=len(completed) / len(attempts) if attempts else 0.0,
        incomplete=len(completed) < len(attempts),
        scenarios_scored=scenarios_scored,
        scenarios_total=len(scenario_ids),
        passed_attempts=len(passed),
        failed_attempts=sum(1 for a in completed if not a.passed),
        cancelled_attempts=sum(1 for a in attempts if a.status == AttemptStatus.CANCELLED),
        errored_attempts=sum(1 for a in attempts if a.status == AttemptStatus.ERRORED),
        pass_at_1=len(passed) / len(completed) if completed else 0.0,
        pass_hat_k=scenarios_all_passed / scenarios_scored if scenarios_scored else 0.0,
        axis_scores=_axis_means(completed),
        cost_usd=_cost(completed),
        latency_p50_ms=percentile(all_latencies, 0.5),
        latency_p95_ms=percentile(all_latencies, 0.95),
        queue_wait_total_ms=int(sum(waits)),
        queue_wait_p95_ms=percentile([float(w) for w in waits], 0.95),
        rate_limit_bypasses=bypasses,
        model_calls=len(all_latencies),
        input_tokens=sum(a.input_tokens for a in attempts),
        output_tokens=sum(a.output_tokens for a in attempts),
        mean_steps_per_turn=mean(float(count) for _, count in steps),
        per_scenario=per_scenario,
    )
