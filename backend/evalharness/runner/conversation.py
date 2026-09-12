"""The conversation loop (SPEC 6.1).

One call to :func:`run_attempt` executes one scenario x one repetition and
returns everything that happened: turns, tool calls, graded assertions and an
ordered event log. The event log is the audit trail (SPEC 8.3) -- it is
append-only and never rewritten after the fact, which is why grading reads the
records rather than mutating them.

Nothing here decides *whether* behavior was correct; that is the grader's job.
This module only drives the conversation and writes down what happened.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from evalharness.engine import ToolEngine
from evalharness.engine.matching import match_args
from evalharness.grader import grade_scenario_scope, grade_turn, score_axes
from evalharness.grader.judge import grade_judge_assertions
from evalharness.grader.scoring import attempt_passed, critical_failures, total_cost
from evalharness.loader.scenario_loader import LoadedScenario
from evalharness.runner.provider import Provider, RateLimited, RetryReporting
from evalharness.schema.enums import EventType
from evalharness.schema.runtime import (
    AssertionResult,
    AssistantMessage,
    AttemptResult,
    Event,
    ToolCall,
    ToolCallRecord,
    ToolResult,
    TurnRecord,
)
from evalharness.schema.scenario import Scenario, Turn

#: The one line the harness prepends. Scenario authors control everything else.
CLOCK_LINE = "Current date and time: {clock}."


def format_clock(moment: datetime) -> str:
    """ISO 8601 with the weekday spelled out, e.g. ``Monday 2026-03-02T09:00:00+04:00``."""
    return f"{moment:%A} {moment.isoformat()}"


def build_system_prompt(scenario: Scenario) -> str:
    """The clock line, a newline, then the scenario's own system prompt (SPEC 4.1)."""
    return CLOCK_LINE.format(clock=format_clock(scenario.clock)) + "\n" + scenario.system_prompt


@dataclass
class AttemptContext:
    """Everything :func:`run_attempt` needs to execute one attempt."""

    loaded: LoadedScenario
    provider: Provider
    repetition: int
    cancel_check: Callable[[], Awaitable[bool]] | None = None
    """Polled between model calls (SPEC 9.1). Returning true stops the attempt."""
    on_event: Callable[[Event], Awaitable[None]] | None = None
    """Called as each event is recorded, for SSE progress streaming."""
    judge_provider: Provider | None = None
    """Only used when ``EVALHARNESS_ENABLE_JUDGE`` is set (SPEC 4.3)."""
    params: dict[str, Any] = field(default_factory=dict)
    """Extra provider params (a run's ``params_override``)."""
    rate_limit: Callable[[], Awaitable[tuple[float, bool]]] | None = None
    """Awaited before every model call; returns (seconds queued, bypassed) (D27)."""


def _jsonify(value: Any) -> Any:
    """Make a value safe to store in an event payload."""
    return json.loads(json.dumps(value, default=str))


def _tool_message_content(result: ToolResult) -> str:
    payload = result.content if result.content is not None else {"error": result.error or "error"}
    return json.dumps(payload, default=str)


def _assistant_message_dict(message: AssistantMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments
                    if call.raw_arguments is not None
                    else json.dumps(call.arguments, default=str),
                },
            }
            for call in message.tool_calls
        ]
    return out


class _Attempt:
    """Mutable state for one attempt. One instance per :func:`run_attempt` call."""

    def __init__(self, ctx: AttemptContext) -> None:
        self.ctx = ctx
        self.loaded = ctx.loaded
        self.scenario: Scenario = ctx.loaded.scenario
        self.engine = ToolEngine(ctx.loaded)
        self.engine.reset()
        self.events: list[Event] = []
        self.turns: list[TurnRecord] = []
        self.assertion_results: list[AssertionResult] = []
        self.costs: list[float | None] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.seq = 0
        self.error: str | None = None
        self.cancelled = False
        self.stop = False
        self.truncated = False
        # Real elapsed time, waits included. turn_timeout_s deliberately does not
        # count queueing, so this is what stops a starved attempt (D33).
        self.attempt_deadline = time.monotonic() + self.scenario.limits.attempt_timeout_s

    # -- events ------------------------------------------------------------

    async def record(
        self,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        turn_index: int | None = None,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> Event:
        event = Event(
            seq=self.seq,
            turn_index=turn_index,
            type=event_type,
            payload=_jsonify(payload),
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=datetime.now(UTC),
        )
        self.seq += 1
        self.events.append(event)
        if self.ctx.on_event is not None:
            await self.ctx.on_event(event)
        return event

    async def on_retry(
        self,
        *,
        attempt: int,
        max_attempts: int,
        delay_s: float,
        error: str,
        status_code: int | None,
    ) -> None:
        """Provider retry hook -- retries belong in the trace (SPEC 6.4)."""
        await self.record(
            EventType.RETRY,
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_s": delay_s,
                "error": error,
                "status_code": status_code,
            },
            turn_index=self.turns[-1].index if self.turns else None,
        )

    # -- control -----------------------------------------------------------

    async def cancelled_now(self) -> bool:
        if self.ctx.cancel_check is None:
            return False
        return await self.ctx.cancel_check()

    @property
    def all_records(self) -> list[ToolCallRecord]:
        return [record for turn in self.turns for record in turn.tool_calls]

    def exposure_met(self) -> bool | None:
        """Did this attempt meet the scenario's exposure condition? (D38)

        None when the scenario declares no probe. Otherwise true only if some
        call actually matched it -- an attempt that stopped before reaching the
        injected page did not resist anything, and must not be counted among
        the clean negatives.
        """
        probe = self.scenario.exposure
        if probe is None:
            return None
        return any(
            record.name == probe.tool and match_args(probe.args, record.arguments).matched
            for record in self.all_records
        )

    # -- main loop ---------------------------------------------------------

    async def run(self) -> AttemptResult:
        started = time.monotonic()
        system_prompt = build_system_prompt(self.scenario)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        await self.record(
            EventType.SYSTEM,
            {
                "system_prompt": system_prompt,
                "clock": self.scenario.clock.isoformat(),
                "tools": sorted(self.loaded.tool_names),
                "repetition": self.ctx.repetition,
            },
        )

        # Providers retry internally, but the retries belong in this attempt's
        # trace, so hand them somewhere to report (SPEC 6.4).
        reporter = self.ctx.provider if isinstance(self.ctx.provider, RetryReporting) else None
        if reporter is not None:
            reporter.set_retry_hook(self.on_retry)

        # The limiter goes on the provider so retries take slots too (D32).
        throttled = self.ctx.provider if isinstance(self.ctx.provider, RateLimited) else None
        if throttled is not None and self.ctx.rate_limit is not None:
            throttled.set_rate_limit_hook(self.ctx.rate_limit)
        try:
            for index, config_turn in enumerate(self.scenario.turns):
                if await self.cancelled_now():
                    await self.mark_cancelled(index)
                    break
                await self.run_one_turn(index, config_turn, messages)
                if self.stop:
                    break
        finally:
            if reporter is not None:
                reporter.set_retry_hook(None)
            if throttled is not None:
                throttled.set_rate_limit_hook(None)

        return await self.finish(started)

    async def mark_cancelled(self, turn_index: int | None) -> None:
        self.cancelled = True
        self.stop = True
        self.error = "cancelled"
        await self.record(EventType.ERROR, {"cancelled": True}, turn_index=turn_index)

    async def run_one_turn(
        self, index: int, config_turn: Turn, messages: list[dict[str, Any]]
    ) -> None:
        turn = TurnRecord(index=index, user_message=config_turn.user)
        self.turns.append(turn)
        messages.append({"role": "user", "content": config_turn.user})
        await self.record(EventType.USER_MESSAGE, {"content": config_turn.user}, turn_index=index)

        await self.drive_turn(turn, messages)

        # SPEC 6.1 step 5: grade this turn's turn-scoped assertions.
        results = grade_turn(self.loaded, turn, self.all_records)
        self.assertion_results.extend(results)
        turn.passed = attempt_passed(results)

        if critical_failures(results):
            # SPEC 6.1 step 6: a critical failure stops the attempt. Everything
            # recorded so far stays recorded.
            self.stop = True
            return
        if not self.scenario.continue_on_fail and not turn.passed:
            self.stop = True

    async def drive_turn(self, turn: TurnRecord, messages: list[dict[str, Any]]) -> None:
        """Model call -> tool calls -> model call, until it stops or a limit fires.

        The turn budget measures the model, not the harness. Time spent queueing
        for a rate-limit slot is added back to the deadline once the call
        returns, and each call is handed only the model-facing remainder as its
        own timeout -- so a slot that took 55 seconds to obtain no longer eats
        half the turn (DECISIONS D33).
        """
        limits = self.scenario.limits
        deadline = time.monotonic() + limits.turn_timeout_s

        while True:
            if turn.steps >= limits.max_steps_per_turn:
                await self.limit_hit(
                    turn, "max_steps_per_turn", f"{limits.max_steps_per_turn} model calls"
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self.limit_hit(turn, "turn_timeout_s", f"{limits.turn_timeout_s}s elapsed")
                return
            attempt_remaining = self.attempt_deadline - time.monotonic()
            if attempt_remaining <= 0:
                await self.limit_hit(
                    turn,
                    "attempt_timeout_s",
                    f"{limits.attempt_timeout_s}s of wall clock, waits included",
                )
                return
            if await self.cancelled_now():
                await self.mark_cancelled(turn.index)
                return

            message = await self.call_model(turn, messages, min(remaining, attempt_remaining))
            if message is None:
                return

            # Give back whatever the throttle took. wait_ms covers queueing for a
            # slot and any retry backoff: neither is the model spending its turn.
            deadline += message.wait_ms / 1000.0

            messages.append(_assistant_message_dict(message))
            if message.content:
                turn.final_response = message.content
            if not message.tool_calls:
                return

            await self.execute_calls(turn, message.tool_calls, messages)

    async def call_model(
        self, turn: TurnRecord, messages: list[dict[str, Any]], budget_s: float
    ) -> AssistantMessage | None:
        """One model call. Returns ``None`` when the turn must end."""
        await self.record(
            EventType.MODEL_REQUEST,
            {
                "step": turn.steps,
                "messages": len(messages),
                "tools": len(self.loaded.tools),
                "tool_choice": "auto",
                "provider": self.ctx.provider.name,
            },
            turn_index=turn.index,
        )
        try:
            # The timeout is handed to the provider rather than wrapped around
            # it, so it bounds the HTTP call alone. Wrapping the whole coroutine
            # charged rate-limiter queueing to the turn budget, and at rpm=12 a
            # p95 queue wait of ~55s against a 120s turn meant two queued calls
            # timed out a turn the model was handling fine (DECISIONS D33).
            message = await self.ctx.provider.complete(
                messages,
                self.loaded.openai_tools(),
                tool_choice="auto",
                call_timeout_s=budget_s,
                **self.ctx.params,
            )
        except TimeoutError:
            await self.limit_hit(turn, "turn_timeout_s", "model call exceeded the turn budget")
            return None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.stop = True
            await self.record(
                EventType.ERROR,
                {"error": self.error, "phase": "model_call", "step": turn.steps},
                turn_index=turn.index,
            )
            return None

        turn.steps += 1
        self.input_tokens += message.input_tokens
        self.output_tokens += message.output_tokens
        self.costs.append(message.cost_usd)
        await self.record(
            EventType.MODEL_RESPONSE,
            {
                "step": turn.steps - 1,
                "content": message.content,
                "finish_reason": message.finish_reason,
                # Which host served this call. The trace is the only place that
                # can prove a pinned run was not silently re-routed (D20).
                "provider": message.provider,
                # Kept apart from latency_ms on purpose: percentiles over
                # latency describe the model, not the throttle in front of it.
                "wait_ms": message.wait_ms,
                # True when the throttle was skipped: unreachable, or we gave up
                # waiting. Counted per run so silent degradation shows up (D31).
                "rate_limit_bypassed": message.rate_limit_bypassed,
                # The invariant: one slot per HTTP request. A shortfall means
                # requests went out unshaped (D32), and is surfaced on the run.
                "http_requests": message.http_requests,
                "rate_limit_acquires": message.rate_limit_acquires,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in message.tool_calls
                ],
                "cost_usd": message.cost_usd,
                # Mirrored from the event columns so a payload-only consumer --
                # a JSON export, the trace viewer -- can size a max_tokens
                # budget without a second query. reasoning_tokens has no column
                # of its own (SPEC 8.3 predates it) and lives only here.
                "input_tokens": message.input_tokens,
                "output_tokens": message.output_tokens,
                "reasoning_tokens": message.reasoning_tokens,
            },
            turn_index=turn.index,
            latency_ms=message.latency_ms,
            input_tokens=message.input_tokens,
            output_tokens=message.output_tokens,
        )
        if message.finish_reason == "length":
            # The model was cut off mid-thought by max_tokens. Distinct from a
            # limit: nothing the harness enforces stopped it, so it would
            # otherwise be graded as if the model simply chose to stop (D35).
            self.truncated = True
            await self.record(
                EventType.TRUNCATED,
                {
                    "step": turn.steps - 1,
                    "output_tokens": message.output_tokens,
                    "reasoning_tokens": message.reasoning_tokens,
                    "detail": "finish_reason=length: the response hit max_tokens",
                },
                turn_index=turn.index,
            )
        return message

    async def execute_calls(
        self, turn: TurnRecord, calls: list[ToolCall], messages: list[dict[str, Any]]
    ) -> None:
        """Run each tool call through the engine, in order (SPEC 6.1 step 3)."""
        step = turn.steps - 1
        for batch_index, call in enumerate(calls):
            call_event = await self.record(
                EventType.TOOL_CALL,
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "raw_arguments": call.raw_arguments,
                    "parse_error": call.parse_error,
                    "step": step,
                    "batch_index": batch_index,
                },
                turn_index=turn.index,
            )

            result, faulted = await self.execute_one(turn, call)
            record = ToolCallRecord(
                seq=call_event.seq,
                turn_index=turn.index,
                step=step,
                batch_index=batch_index,
                call=call,
                result=result,
                faulted=faulted,
            )
            turn.tool_calls.append(record)
            await self.record(
                EventType.TOOL_RESULT,
                {
                    "id": call.id,
                    "name": call.name,
                    "ok": result.ok,
                    "error": result.error,
                    "content": result.content,
                    "faulted": faulted,
                    "step": step,
                },
                turn_index=turn.index,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": _tool_message_content(result),
                }
            )

    async def execute_one(self, turn: TurnRecord, call: ToolCall) -> tuple[ToolResult, bool]:
        if call.parse_error is not None:
            # A model that emits unparseable arguments gets a realistic error back
            # instead of crashing the attempt (SPEC 5.1).
            return (
                ToolResult(
                    ok=False,
                    error="invalid_arguments",
                    content={
                        "error": "invalid_arguments",
                        "message": call.parse_error,
                        "raw_arguments": call.raw_arguments,
                    },
                ),
                False,
            )

        outcome = self.engine.execute(call.name, call.arguments)
        if outcome.faulted and outcome.fault is not None:
            await self.record(
                EventType.FAULT,
                {
                    "tool": outcome.fault.tool,
                    "on_call": outcome.fault.on_call,
                    "response": outcome.fault.response,
                },
                turn_index=turn.index,
            )
        return outcome.result, outcome.faulted

    async def limit_hit(self, turn: TurnRecord, reason: str, detail: str) -> None:
        """SPEC 6.1 step 4: a limit ends the turn and is recorded."""
        turn.limit_exceeded = True
        turn.limit_reason = reason
        await self.record(
            EventType.LIMIT_EXCEEDED,
            {"limit": reason, "detail": detail, "steps": turn.steps},
            turn_index=turn.index,
        )

    # -- finish ------------------------------------------------------------

    async def finish(self, started: float) -> AttemptResult:
        # SPEC 6.1 step 7: scenario-scoped assertions see the whole attempt.
        result = AttemptResult(
            scenario_id=self.scenario.id,
            repetition=self.ctx.repetition,
            turns=self.turns,
            events=self.events,
            truncated=self.truncated,
            exposed=self.exposure_met(),
        )
        if not self.cancelled:
            self.assertion_results.extend(grade_scenario_scope(self.loaded, result))
            self.assertion_results.extend(
                await grade_judge_assertions(self.loaded, result, self.ctx.judge_provider)
            )

        result.assertion_results = self.assertion_results
        result.critical_failure = bool(critical_failures(self.assertion_results))
        result.passed = attempt_passed(self.assertion_results) and self.error is None
        result.axis_scores = score_axes(self.assertion_results)
        result.cost_usd = total_cost(self.costs)
        result.input_tokens = self.input_tokens
        result.output_tokens = self.output_tokens
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.error = self.error
        # A model call that failed after its retries leaves the attempt without a
        # verdict. Recording it as a plain failure would blame the model for an
        # outage, so it is marked ungraded and kept out of the rates entirely.
        result.errored = self.error is not None
        self.engine.reset()
        return result


async def run_attempt(ctx: AttemptContext) -> AttemptResult:
    """Execute one scenario x one repetition and grade it (SPEC 6.1)."""
    return await _Attempt(ctx).run()
