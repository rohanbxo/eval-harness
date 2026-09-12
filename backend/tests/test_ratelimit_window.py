"""The rate limiter's real invariant (DECISIONS D32).

The previous implementation passed its own unit tests and still let a run make
28-35 requests per rolling minute against a cap of 20. Those tests asserted the
bucket's internal arithmetic; they never asserted the thing a provider actually
measures.

These tests assert that instead: **the measured peak in any rolling 60-second
window must not exceed rpm**, and they do it across processes, because a
per-process limiter satisfies a single-process test while failing in production.
`test_a_token_bucket_fails_this_suite` proves the assertion catches the old bug.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Sequence

import pytest

from evalharness.worker.ratelimit import (
    GUARD_SECONDS,
    WINDOW_SECONDS,
    MemoryRateLimiter,
    RateLimiter,
    acquire_detailed,
)


def peak_in_any_window(stamps: Sequence[float], window: float = WINDOW_SECONDS) -> int:
    """Largest number of timestamps falling inside any `window`-second span.

    This is what a provider counts, so it is what the tests assert.
    """
    ordered = sorted(stamps)
    peak = 0
    start = 0
    for end in range(len(ordered)):
        while ordered[end] - ordered[start] >= window:
            start += 1
        peak = max(peak, end - start + 1)
    return peak


class TokenBucketLimiter(RateLimiter):
    """The implementation D32 replaced, kept only to prove the tests catch it.

    Capacity equals the rate, so a full bucket drains as a burst and then admits
    each refill: roughly 2x the rate inside one rolling window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    async def take(self, model_key: str, rpm: int) -> float:
        if rpm <= 0:
            return 0.0
        rate = rpm / 60.0
        now = time.time()
        with self._lock:
            tokens, updated = self._buckets.get(model_key, (float(rpm), now))
            tokens = min(float(rpm), tokens + (now - updated) * rate)
            if tokens >= 1:
                self._buckets[model_key] = (tokens - 1, now)
                return 0.0
            self._buckets[model_key] = (tokens, now)
            return (1 - tokens) / rate


class VirtualClock:
    """Compresses a 60-second window into test time.

    Real sleeping would make these tests minutes long; the limiter only ever
    reads `time.time`, so advancing it is equivalent.
    """

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self._lock = threading.Lock()

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    virtual = VirtualClock()
    monkeypatch.setattr("evalharness.worker.ratelimit.time.time", virtual.time)
    return virtual


async def drive(
    limiter: RateLimiter, clock: VirtualClock, *, requests: int, rpm: int, workers: int = 4
) -> list[float]:
    """Run `requests` acquisitions across `workers` concurrent tasks."""
    stamps: list[float] = []
    queue = asyncio.Queue[int]()
    for i in range(requests):
        queue.put_nowait(i)

    async def worker() -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await acquire_detailed("m", rpm, limiter=limiter, sleep=clock.sleep, max_wait_s=10_000)
            stamps.append(clock.now)

    await asyncio.gather(*(worker() for _ in range(workers)))
    return stamps


# --------------------------------------------------------------------------- #
# The invariant                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rpm", [5, 12, 20])
async def test_peak_never_exceeds_rpm(clock: VirtualClock, rpm: int) -> None:
    stamps = await drive(MemoryRateLimiter(), clock, requests=rpm * 4, rpm=rpm)
    peak = peak_in_any_window(stamps)
    assert peak <= rpm, f"peak {peak} in a rolling minute exceeds rpm={rpm}"


async def test_a_token_bucket_fails_this_suite(clock: VirtualClock) -> None:
    """Proof the assertion above has teeth.

    The replaced implementation admits its whole capacity at once and then each
    refill, so it breaches the window it claims to enforce. If this test ever
    passes, the invariant test above has stopped testing anything.
    """
    stamps = await drive(TokenBucketLimiter(), clock, requests=48, rpm=12)
    peak = peak_in_any_window(stamps)
    assert peak > 12, (
        f"the old token bucket was expected to exceed rpm in a rolling window; it peaked at {peak}"
    )


async def test_the_burst_itself_is_bounded(clock: VirtualClock) -> None:
    """A fresh window must not admit more than rpm immediately."""
    limiter = MemoryRateLimiter()
    admitted = 0
    for _ in range(50):
        if await limiter.take("m", 12) <= 0:
            admitted += 1
        else:
            break
    assert admitted == 12


async def test_requests_resume_once_the_window_rolls(clock: VirtualClock) -> None:
    limiter = MemoryRateLimiter()
    for _ in range(12):
        assert await limiter.take("m", 12) == 0.0
    assert await limiter.take("m", 12) > 0

    # Entries are held for the window plus the guard band, so a slot frees a
    # little later than the nominal 60s -- deliberately conservative (D32).
    clock.now += WINDOW_SECONDS + GUARD_SECONDS + 1
    assert await limiter.take("m", 12) == 0.0


async def test_models_are_shaped_independently(clock: VirtualClock) -> None:
    limiter = MemoryRateLimiter()
    for _ in range(12):
        await limiter.take("model-a", 12)
    assert await limiter.take("model-a", 12) > 0
    assert await limiter.take("model-b", 12) == 0.0


# --------------------------------------------------------------------------- #
# Across processes                                                             #
# --------------------------------------------------------------------------- #


def _hammer(args: tuple[str, str, int, int]) -> tuple[list[float], int]:
    """Child-process body: take `count` slots, reporting when each was admitted.

    Also counts bypasses. The limiter fails open by design (D31), so an
    unreachable or overloaded Redis does not raise here -- it quietly lets every
    request through, and the window assertion downstream then reports "the
    limiter is not shared" for what is actually an infrastructure fault. The
    count is returned so the test can tell those two apart (D45).
    """
    redis_url, model_key, rpm, count = args
    import asyncio as child_asyncio

    from evalharness.worker.ratelimit import RedisRateLimiter, acquire_detailed

    async def run() -> tuple[list[float], int]:
        limiter = RedisRateLimiter(redis_url)
        stamps: list[float] = []
        bypasses = 0
        for _ in range(count):
            outcome = await acquire_detailed(model_key, rpm, limiter=limiter, max_wait_s=90)
            bypasses += int(outcome.bypassed)
            stamps.append(time.time())
        await limiter.close()
        return stamps, bypasses

    return child_asyncio.run(run())


def _redis_is_reachable(url: str) -> str | None:
    """``None`` when Redis answers, otherwise why it did not.

    Checked explicitly because every other symptom of an unreachable Redis in
    this test looks like a limiter bug (D45).
    """
    import asyncio as check_asyncio

    async def ping() -> str | None:
        import redis.asyncio as redis

        client = redis.from_url(url)  # type: ignore[no-untyped-call]
        try:
            await client.ping()
            return None
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            await client.aclose()

    return check_asyncio.run(ping())


@pytest.mark.skipif(
    not os.environ.get("EVALHARNESS_TEST_REDIS_URL"),
    reason="needs a real Redis; set EVALHARNESS_TEST_REDIS_URL",
)
def test_separate_processes_share_one_window() -> None:
    """The test a per-process limiter fails.

    Four OS processes, one model, rpm=12. If each process kept its own window
    they would collectively admit ~48 immediately; sharing Redis they cannot
    exceed 12 in any rolling minute.
    """
    import multiprocessing

    redis_url = os.environ["EVALHARNESS_TEST_REDIS_URL"]

    # Fail loudly rather than skipping: a skip here is indistinguishable from
    # the test never having existed, which is the defect D39 closed. But fail
    # with the *right* reason -- every downstream symptom of an unreachable
    # Redis in this test reads as "the limiter is broken" (D45).
    unreachable = _redis_is_reachable(redis_url)
    assert unreachable is None, (
        f"Redis at {redis_url} did not answer PING ({unreachable}). This test cannot "
        "measure a shared window without it. In CI, check the service container is "
        "healthy and its port is mapped; nothing below would be a limiter defect."
    )

    model_key = f"multiproc-{int(time.time())}"
    rpm = 12

    with multiprocessing.Pool(4) as pool:
        results = pool.map(_hammer, [(redis_url, model_key, rpm, 6)] * 4)

    stamps = [s for batch, _ in results for s in batch]
    bypasses = sum(count for _, count in results)
    assert len(stamps) == 24

    # Checked before the window assertion, because a bypass *causes* a window
    # breach: the limiter fails open, the request goes out unshaped, and the
    # peak then exceeds rpm for a reason that has nothing to do with sharing.
    assert bypasses == 0, (
        f"{bypasses} of 24 acquisitions bypassed the limiter, so this run cannot "
        "test whether the window is shared. A bypass means Redis was unreachable "
        "mid-run, or an acquire waited past its 90s ceiling on a contended runner "
        "-- neither is a limiter defect, and both would otherwise surface below as "
        "'the limiter is not shared'."
    )

    peak = peak_in_any_window(stamps)
    assert peak <= rpm, (
        f"{peak} requests landed in one rolling minute across 4 processes, "
        f"above rpm={rpm}: the limiter is not shared"
    )
