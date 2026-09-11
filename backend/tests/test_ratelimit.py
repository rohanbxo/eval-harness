"""Per-model request-rate limiting (DECISIONS D27, D28).

Providers cap requests per minute *per model*, so the harness shapes the rate per
model rather than serialising the whole run — four models can still run at once.
These tests pin the bucket's arithmetic, that the throttle never blocks a run
when it is itself broken, and that queue time is reported apart from latency so
percentiles keep describing the model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from evalharness.worker.ratelimit import (
    BUCKET_KEY,
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
    """Controllable time, so a 60-second window costs no real seconds."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------- #
# The bucket                                                                   #
# --------------------------------------------------------------------------- #


async def test_a_fresh_bucket_is_full() -> None:
    """A run should not be throttled before it has made any requests."""
    limiter = MemoryRateLimiter()
    for _ in range(15):
        assert await limiter.take("m", 15) == 0.0


async def test_the_bucket_empties_and_then_asks_you_to_wait() -> None:
    limiter = MemoryRateLimiter()
    for _ in range(15):
        await limiter.take("m", 15)
    wait = await limiter.take("m", 15)
    assert wait > 0
    # At 15/min a token arrives every 4s; the whole minute is never demanded.
    assert wait <= 4.0


async def test_models_are_throttled_independently() -> None:
    """The entire point: four models in parallel, each under its own cap."""
    limiter = MemoryRateLimiter()
    for _ in range(15):
        await limiter.take("model-a", 15)
    assert await limiter.take("model-a", 15) > 0
    assert await limiter.take("model-b", 15) == 0.0


async def test_tokens_refill_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = MemoryRateLimiter()
    clock = FakeClock()
    monkeypatch.setattr("evalharness.worker.ratelimit.time.time", lambda: clock.now)

    for _ in range(15):
        await limiter.take("m", 15)
    assert await limiter.take("m", 15) > 0

    clock.now += 8.0  # two tokens at 15/min
    assert await limiter.take("m", 15) == 0.0
    assert await limiter.take("m", 15) == 0.0
    assert await limiter.take("m", 15) > 0


async def test_refill_is_capped_at_the_bucket_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle hour must not license an hour's worth of burst."""
    limiter = MemoryRateLimiter()
    clock = FakeClock()
    monkeypatch.setattr("evalharness.worker.ratelimit.time.time", lambda: clock.now)

    await limiter.take("m", 15)
    clock.now += 3600
    allowed = 0
    for _ in range(100):
        if await limiter.take("m", 15) == 0.0:
            allowed += 1
        else:
            break
    assert allowed == 15


async def test_rpm_zero_disables_throttling() -> None:
    limiter = MemoryRateLimiter()
    for _ in range(50):
        assert await limiter.take("m", 0) == 0.0


# --------------------------------------------------------------------------- #
# acquire()                                                                    #
# --------------------------------------------------------------------------- #


async def test_acquire_returns_immediately_when_a_token_is_free() -> None:
    assert await acquire("m", 15, limiter=MemoryRateLimiter()) == 0.0


async def test_acquire_waits_and_reports_the_wait() -> None:
    limiter = MemoryRateLimiter()
    clock = FakeClock()
    for _ in range(15):
        await limiter.take("m", 15)

    waited = await acquire("m", 15, limiter=limiter, sleep=clock.sleep, max_wait_s=10)
    assert waited > 0
    assert clock.slept, "it must actually sleep rather than spin"


async def test_a_broken_limiter_never_blocks_the_run() -> None:
    """If the throttle is down, run slightly too fast rather than not at all —
    the provider's Retry-After backoff is the backstop (D27)."""

    class Broken(RateLimiter):
        async def take(self, model_key: str, rpm: int) -> float:
            raise ConnectionError("redis is down")

    assert await acquire("m", 15, limiter=Broken()) == 0.0


async def test_acquire_gives_up_after_max_wait() -> None:
    """A pathological bucket must not wedge a run forever."""

    class AlwaysBusy(RateLimiter):
        async def take(self, model_key: str, rpm: int) -> float:
            return 999.0

    clock = FakeClock()
    waited = await acquire("m", 1, limiter=AlwaysBusy(), sleep=clock.sleep, max_wait_s=1.0)
    assert waited >= 1.0


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


def test_the_redis_bucket_key_is_per_model() -> None:
    assert BUCKET_KEY.format(model_key="a") != BUCKET_KEY.format(model_key="b")
    assert "a" in BUCKET_KEY.format(model_key="a")


async def test_redis_limiter_uses_the_script() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def eval(self, script: str, numkeys: int, *args: Any) -> str:
            self.calls.append(args)
            return "0"

    limiter = RedisRateLimiter("redis://localhost:6379/0")
    client = FakeRedis()
    limiter._clients[id(asyncio.get_running_loop())] = client

    assert await limiter.take("gpt-5.6-terra", 15) == 0.0
    assert client.calls[0][0] == BUCKET_KEY.format(model_key="gpt-5.6-terra")
    assert client.calls[0][1] == 15


# --------------------------------------------------------------------------- #
# End to end with FakeModel, under a simulated cap                             #
# --------------------------------------------------------------------------- #


async def test_a_full_attempt_is_throttled_and_reports_its_wait() -> None:
    """FakeModel under a 2 rpm cap: the attempt still completes and passes, and
    every call after the first two has to queue for a token."""
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel
    from evalharness.runner.conversation import AttemptContext, run_attempt

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]

    limiter = MemoryRateLimiter()
    clock = FakeClock()
    calls = 0

    async def rate_limit() -> tuple[float, bool]:
        nonlocal calls
        calls += 1
        # 2 rpm: a token every 30s, so only the first two calls go straight out.
        outcome = await acquire_detailed(
            "capped", 2, limiter=limiter, sleep=clock.sleep, max_wait_s=600
        )
        return (outcome.waited_s, outcome.bypassed)

    result = await run_attempt(
        AttemptContext(
            loaded=loaded, provider=FakeModel(golden), repetition=0, rate_limit=rate_limit
        )
    )

    assert result.passed is True, "throttling must not change the verdict"
    assert calls >= 3, "the golden transcript makes several model calls"
    assert clock.slept, "calls past the cap must have queued"

    # The wait is recorded, and kept out of latency.
    responses = [e for e in result.events if e.type.value == "model_response"]
    waits = [v for e in responses if isinstance(v := e.payload.get("wait_ms"), int)]
    assert sum(waits) > 0, "queue time must be recorded on the events"
    assert all((e.latency_ms or 0) >= 0 for e in responses)


async def test_an_unthrottled_attempt_records_no_wait() -> None:
    """With rpm high enough, nothing queues and wait_ms stays zero."""
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel
    from evalharness.runner.conversation import AttemptContext, run_attempt

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    limiter = MemoryRateLimiter()

    async def rate_limit() -> tuple[float, bool]:
        outcome = await acquire_detailed("roomy", 600, limiter=limiter)
        return (outcome.waited_s, outcome.bypassed)

    result = await run_attempt(
        AttemptContext(
            loaded=loaded, provider=FakeModel(golden), repetition=0, rate_limit=rate_limit
        )
    )
    assert result.passed is True
    responses = [e for e in result.events if e.type.value == "model_response"]
    assert all(e.payload.get("wait_ms") == 0 for e in responses)


async def test_an_attempt_without_a_limiter_still_runs() -> None:
    """rate_limit is optional; omitting it must not change behaviour."""
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel
    from evalharness.runner.conversation import AttemptContext, run_attempt

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_attempt(
        AttemptContext(loaded=loaded, provider=FakeModel(golden), repetition=0)
    )
    assert result.passed is True
