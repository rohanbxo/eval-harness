"""Rate-limiter behaviour around the edges (DECISIONS D27, D31, D32).

The *invariant* — peak requests in any rolling minute, including across
processes — lives in `test_ratelimit_window.py`. This file covers what happens
when the limiter is unavailable, how bypasses are reported, and that a throttled
attempt still runs correctly end to end under FakeModel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from evalharness.worker.ratelimit import (
    WINDOW_KEY,
    Acquisition,
    MemoryRateLimiter,
    RateLimiter,
    RedisRateLimiter,
    acquire,
    acquire_detailed,
    get_rate_limiter,
    set_rate_limiter,
)

TRAVEL_BOOKING = Path(__file__).resolve().parents[2] / "scenarios" / "travel-booking"


@pytest.fixture(autouse=True)
def _isolate_limiter() -> Any:
    set_rate_limiter(None)
    yield
    set_rate_limiter(None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------- #
# acquire()                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_free_slot_is_taken_immediately() -> None:
    assert await acquire("m", 12, limiter=MemoryRateLimiter()) == 0.0


async def test_rpm_zero_disables_throttling() -> None:
    """FakeModel entries use this: no request made, so no rate to shape."""
    limiter = MemoryRateLimiter()
    for _ in range(50):
        assert await limiter.take("m", 0) == 0.0


async def test_a_full_window_makes_the_caller_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = MemoryRateLimiter()
    clock = FakeClock()
    monkeypatch.setattr("evalharness.worker.ratelimit.time.time", lambda: clock.now)
    for _ in range(12):
        await limiter.take("m", 12)

    outcome = await acquire_detailed("m", 12, limiter=limiter, sleep=clock.sleep, max_wait_s=200)
    assert outcome.waited_s > 0
    assert outcome.bypassed is False
    assert clock.slept, "it must sleep rather than spin"


# --------------------------------------------------------------------------- #
# Failing open, but visibly (D31)                                              #
# --------------------------------------------------------------------------- #


async def test_a_broken_limiter_never_blocks_the_run() -> None:
    """Running slightly too fast beats not running; the backoff is the backstop."""

    class Broken(RateLimiter):
        async def take(self, model_key: str, rpm: int) -> float:
            raise ConnectionError("redis is down")

    outcome = await acquire_detailed("m", 12, limiter=Broken())
    assert outcome.waited_s == 0.0
    assert outcome.bypassed is True, "a bypass must be reported, not silently swallowed"


async def test_giving_up_after_max_wait_counts_as_a_bypass() -> None:
    class AlwaysBusy(RateLimiter):
        async def take(self, model_key: str, rpm: int) -> float:
            return 999.0

    clock = FakeClock()
    outcome = await acquire_detailed(
        "m", 1, limiter=AlwaysBusy(), sleep=clock.sleep, max_wait_s=1.0
    )
    assert outcome.waited_s >= 1.0
    assert outcome.bypassed is True


async def test_a_normal_acquisition_is_not_a_bypass() -> None:
    assert await acquire_detailed("m", 12, limiter=MemoryRateLimiter()) == Acquisition(0.0, False)


# --------------------------------------------------------------------------- #
# Backend selection                                                            #
# --------------------------------------------------------------------------- #


def test_memory_backend_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "memory://")
    from evalharness.config import get_settings

    get_settings.cache_clear()
    try:
        assert isinstance(get_rate_limiter(), MemoryRateLimiter)
    finally:
        get_settings.cache_clear()


def test_redis_backend_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker must not silently fall back to a per-process window."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from evalharness.config import get_settings

    get_settings.cache_clear()
    try:
        assert isinstance(get_rate_limiter(), RedisRateLimiter)
    finally:
        get_settings.cache_clear()


def test_the_window_key_is_per_model() -> None:
    assert WINDOW_KEY.format(model_key="a") != WINDOW_KEY.format(model_key="b")


async def test_redis_members_are_unique_per_request() -> None:
    """Duplicate members would make ZADD update a score rather than add a slot,
    so the window would hold fewer entries than requests actually made."""
    seen: list[str] = []

    class CapturingRedis:
        async def eval(self, script: str, numkeys: int, *args: Any) -> str:
            seen.append(str(args[5]))
            return "0"

    limiter = RedisRateLimiter("redis://localhost:6379/0")
    limiter._clients[id(asyncio.get_running_loop())] = CapturingRedis()
    for _ in range(5):
        await limiter.take("m", 12)
    assert len(set(seen)) == 5


# --------------------------------------------------------------------------- #
# End to end with FakeModel                                                    #
# --------------------------------------------------------------------------- #


async def test_a_throttled_attempt_still_passes() -> None:
    """Under a 2 rpm cap the attempt completes unchanged; only its timing moves."""
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel
    from evalharness.runner.conversation import AttemptContext, run_attempt

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    limiter = MemoryRateLimiter()
    clock = FakeClock()

    async def rate_limit() -> tuple[float, bool]:
        outcome = await acquire_detailed(
            "capped", 2, limiter=limiter, sleep=clock.sleep, max_wait_s=10_000
        )
        return (outcome.waited_s, outcome.bypassed)

    result = await run_attempt(
        AttemptContext(
            loaded=loaded, provider=FakeModel(golden), repetition=0, rate_limit=rate_limit
        )
    )
    assert result.passed is True, "throttling must not change the verdict"


async def test_an_attempt_without_a_limiter_still_runs() -> None:
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel
    from evalharness.runner.conversation import AttemptContext, run_attempt

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_attempt(
        AttemptContext(loaded=loaded, provider=FakeModel(golden), repetition=0)
    )
    assert result.passed is True
