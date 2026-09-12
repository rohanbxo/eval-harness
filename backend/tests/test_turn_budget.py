"""The turn budget measures the model, not the throttle (DECISIONS D33).

``turn_timeout_s`` exists to stop a model that will not finish. For one run it
stopped models that were finishing fine: the rate-limiter acquire had been moved
inside ``provider.complete()`` so that retries would take slots too (D32), and
the conversation loop wrapped that whole coroutine in ``asyncio.wait_for``.
Queueing for a slot therefore spent the turn's budget. At rpm=12 the p95 queue
wait was ~55s against a 120s turn, and 13 of 14 timed-out turns had spent 42-55%
of the budget waiting -- before counting the wait that timed out.

These tests pin the guarantee that broke: a limiter wait longer than the whole
turn budget must not end the turn. ``test_the_old_wrapping_fails_this_suite``
keeps the previous behaviour around to prove the assertion has teeth.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest

from evalharness.loader import load_scenario, load_transcripts
from evalharness.runner import FakeModel
from evalharness.runner.conversation import AttemptContext, run_attempt
from evalharness.schema.runtime import AssistantMessage

TRAVEL_BOOKING = Path(__file__).resolve().parents[2] / "scenarios" / "travel-booking"


class VirtualClock:
    """Wall clock the test advances by hand.

    The loop only ever reads ``time.monotonic``, so simulating a 60-second queue
    wait is a matter of moving the number rather than sleeping.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _advancing_hook(clock: VirtualClock, queue_s: float) -> Any:
    """A rate limiter that always makes the caller wait ``queue_s``."""

    async def hook() -> tuple[float, bool]:
        clock.advance(queue_s)  # queueing burns real wall-clock time
        return (queue_s, False)

    return hook


class ThrottledFakeModel(FakeModel):
    """FakeModel that queues for a slot the way the real provider does.

    The wait happens *inside* ``complete``, which is the arrangement that caused
    the regression: anything wrapping this coroutine in a timeout is timing the
    throttle as well as the model.
    """

    def __init__(self, transcript: Any, clock: VirtualClock, queue_s: float) -> None:
        super().__init__(transcript)
        self.clock = clock
        self.queue_s = queue_s
        self.budgets: list[float | None] = []
        self._hook: Any = None

    def set_rate_limit_hook(self, hook: Any) -> None:
        self._hook = hook

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        call_timeout_s: float | None = None,
        **params: Any,
    ) -> AssistantMessage:
        self.budgets.append(call_timeout_s)
        waited = 0.0
        if self._hook is not None:
            waited, _ = await self._hook()
        message = await super().complete(messages, tools, **params)
        # The model itself is instant; all the elapsed time was queueing.
        message.wait_ms = int(waited * 1000)
        return message


def with_limits(loaded: Any, **updates: float) -> Any:
    """A copy of ``loaded`` with some of its limits changed."""
    limits = loaded.scenario.limits.model_copy(update=updates)
    return dataclasses.replace(
        loaded, scenario=loaded.scenario.model_copy(update={"limits": limits})
    )


def build(
    clock: VirtualClock, queue_s: float, *, attempt_timeout_s: float = 10_000.0
) -> tuple[ThrottledFakeModel, AttemptContext]:
    # The attempt cap is deliberately far out of reach here: these tests are
    # about the *turn* budget, and the cap has its own test below.
    loaded = with_limits(load_scenario(TRAVEL_BOOKING), attempt_timeout_s=attempt_timeout_s)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    provider = ThrottledFakeModel(golden, clock, queue_s)
    ctx = AttemptContext(
        loaded=loaded,
        provider=provider,
        repetition=0,
        rate_limit=_advancing_hook(clock, queue_s),
    )
    return provider, ctx


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    virtual = VirtualClock()
    shim = type("TimeShim", (), {"monotonic": staticmethod(virtual.monotonic)})
    monkeypatch.setattr("evalharness.runner.conversation.time", shim)
    return virtual


# --------------------------------------------------------------------------- #
# The guarantee                                                                #
# --------------------------------------------------------------------------- #


async def test_a_queue_wait_longer_than_the_turn_budget_still_passes(
    clock: VirtualClock,
) -> None:
    """The regression, stated as the thing a provider queue must not do."""
    loaded = load_scenario(TRAVEL_BOOKING)
    budget = loaded.scenario.limits.turn_timeout_s
    queue_s = budget * 2  # one wait alone exceeds the whole turn

    provider, ctx = build(clock, queue_s)
    result = await run_attempt(ctx)

    assert provider.calls > 1, "the scenario must make several calls for this to mean anything"
    assert provider.calls * queue_s > budget, (
        "the test is only meaningful if the waits exceed the budget"
    )
    assert result.passed is True, "throttling must not change the verdict"
    assert not any(turn.limit_exceeded for turn in result.turns), (
        "queueing for a slot must not be charged to turn_timeout_s"
    )


async def test_the_model_is_handed_only_its_own_budget(clock: VirtualClock) -> None:
    """Each call's timeout bounds the model call, so it never exceeds the turn."""
    loaded = load_scenario(TRAVEL_BOOKING)
    budget = loaded.scenario.limits.turn_timeout_s
    provider, ctx = build(clock, queue_s=budget * 2)
    await run_attempt(ctx)

    assert provider.budgets, "the loop must pass a budget down to the provider"
    assert all(b is not None and 0 < b <= budget for b in provider.budgets), provider.budgets


async def test_the_old_wrapping_fails_this_suite(clock: VirtualClock) -> None:
    """Proof the assertion above has teeth.

    Re-creates what the loop used to do -- time the whole coroutine, queue wait
    included -- and shows the turn dying on a model that answered instantly. If
    this ever stops raising, the guarantee is no longer being tested.
    """
    loaded = load_scenario(TRAVEL_BOOKING)
    budget = loaded.scenario.limits.turn_timeout_s
    queue_s = budget * 2
    provider, _ = build(clock, queue_s)
    provider.set_rate_limit_hook(_advancing_hook(clock, queue_s))

    async def old_style_call() -> AssistantMessage:
        deadline = clock.monotonic() + budget
        message = await provider.complete([{"role": "user", "content": "hi"}], [])
        if clock.monotonic() > deadline:
            raise TimeoutError("model call exceeded the turn budget")
        return message

    with pytest.raises(TimeoutError):
        await old_style_call()


# --------------------------------------------------------------------------- #
# The backstop                                                                 #
# --------------------------------------------------------------------------- #


async def test_the_attempt_cap_still_stops_a_starved_attempt(clock: VirtualClock) -> None:
    """Ignoring queue time in the turn budget must not make an attempt immortal.

    ``attempt_timeout_s`` is the one limit measured in real elapsed time, waits
    included, so an attempt that never gets a slot still ends.
    """
    loaded = with_limits(load_scenario(TRAVEL_BOOKING), attempt_timeout_s=5.0)

    provider = ThrottledFakeModel(load_transcripts(TRAVEL_BOOKING)["golden"], clock, 60.0)
    result = await run_attempt(
        AttemptContext(
            loaded=loaded,
            provider=provider,
            repetition=0,
            rate_limit=_advancing_hook(clock, 60.0),
        )
    )

    reasons = [turn.limit_reason for turn in result.turns if turn.limit_exceeded]
    assert "attempt_timeout_s" in reasons, reasons


def test_the_clock_shim_matches_the_real_one() -> None:
    """Guards the fixture: if the loop stops using time.monotonic, say so."""
    assert isinstance(time.monotonic(), float)
