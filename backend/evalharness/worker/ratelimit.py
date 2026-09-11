"""Per-model request-rate limiting (DECISIONS D27, D32).

Providers cap requests per minute *per model*, so the harness shapes the rate per
model rather than serialising the whole run: four models can run in parallel
provided each stays under its own limit.

**Sliding window, not a token bucket.** The first implementation was a token
bucket whose capacity equalled the rate, which permits roughly twice the rate
inside one rolling minute: drain the full burst, then consume each refill as it
lands. Measured against Redis it granted 15 immediately at `rpm=15`, and the real
run peaked at 28-35 requests per minute against a cap of 20. A provider counts a
sliding window, so that is what this counts.

The window lives in a Redis sorted set keyed by model, scored by timestamp, and
the check-and-add is one Lua script so concurrent workers cannot both observe
room and then both take it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

#: Redis key for one model's window.
WINDOW_KEY = "evalharness:rpm:{model_key}"

#: The window every provider quotes its limit over.
WINDOW_SECONDS = 60.0

#: Entries are held this much longer than the nominal window.
#:
#: A slot is scored when it is granted, but the HTTP request leaves a moment
#: later, so the provider's 60-second view is offset from ours by the time spent
#: between the two. Measured, that let a 13th request land inside the provider's
#: window at rpm=12. Holding entries slightly longer makes the limiter
#: deliberately conservative: it may admit marginally fewer than rpm, never
#: more, which is the correct direction for a guard whose whole job is to stay
#: under someone else's cap (DECISIONS D32).
GUARD_SECONDS = 3.0

#: How long an idle window survives before Redis reclaims it.
WINDOW_TTL_SECONDS = 300

#: Longest a single acquire will wait before giving up and letting the call
#: through. The provider's Retry-After backoff then handles any 429.
MAX_WAIT_SECONDS = 180.0

#: Ceiling on one sleep between re-checks. Short enough to keep throughput near
#: the cap when slots free up sooner than predicted.
POLL_SECONDS = 0.5

#: Atomically: evict entries older than the window, count what remains, and add
#: a new entry only if there is room. Returns 0 when admitted, otherwise the
#: seconds until the oldest entry falls out of the window.
#:
#: One script rather than read-then-write, because two workers checking a
#: sorted set concurrently would both see room and both take the last slot.
_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local member = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local used = redis.call('ZCARD', key)

if used < limit then
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, ttl)
  return '0'
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
redis.call('EXPIRE', key, ttl)
if oldest[2] == nil then
  return '0'
end
local wait = (tonumber(oldest[2]) + window) - now
if wait < 0 then wait = 0 end
return tostring(wait)
"""


@dataclass(frozen=True)
class Acquisition:
    """The outcome of one attempt to take a slot."""

    waited_s: float
    bypassed: bool = False
    """True when the call went out unshaped: the limiter failed, or we gave up
    waiting. Counted per run so a quiet degradation is visible (D31)."""


class RateLimiter(ABC):
    """Admits at most ``rpm`` requests per model in any rolling window."""

    @abstractmethod
    async def take(self, model_key: str, rpm: int) -> float:
        """Admit a request, or return the seconds until a slot frees."""

    async def close(self) -> None:  # pragma: no cover - overridden where needed
        return None


class MemoryRateLimiter(RateLimiter):
    """Process-local sliding window.

    Correct for a single process. Across several it under-counts, which is why
    the worker uses Redis -- see ``test_ratelimit.py`` for the multi-process test
    that a per-process limiter fails.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, list[float]] = {}

    async def take(self, model_key: str, rpm: int) -> float:
        if rpm <= 0:
            return 0.0
        now = time.time()
        with self._lock:
            horizon = now - (WINDOW_SECONDS + GUARD_SECONDS)
            stamps = [t for t in self._windows.get(model_key, []) if t > horizon]
            if len(stamps) < rpm:
                stamps.append(now)
                self._windows[model_key] = stamps
                return 0.0
            self._windows[model_key] = stamps
            return max(0.0, (stamps[0] + WINDOW_SECONDS + GUARD_SECONDS) - now)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


class RedisRateLimiter(RateLimiter):
    """Sliding window in Redis, shared by every worker process."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._clients: dict[int, Any] = {}
        self._counter = 0

    def _client(self) -> Any:
        import redis.asyncio as redis

        loop_id = id(asyncio.get_running_loop())
        client = self._clients.get(loop_id)
        if client is None:
            client = redis.from_url(self.url, decode_responses=True)  # type: ignore[no-untyped-call]
            self._clients[loop_id] = client
        return client

    async def take(self, model_key: str, rpm: int) -> float:
        if rpm <= 0:
            return 0.0
        now = time.time()
        # Members must be unique or ZADD updates the existing score instead of
        # adding a slot, silently letting the window hold fewer entries than
        # requests. PID plus counter is unique across processes and calls.
        self._counter += 1
        member = f"{os.getpid()}:{self._counter}:{now:.6f}"
        raw = await self._client().eval(
            _ACQUIRE_SCRIPT,
            1,
            WINDOW_KEY.format(model_key=model_key),
            rpm,
            now,
            WINDOW_SECONDS + GUARD_SECONDS,
            WINDOW_TTL_SECONDS,
            member,
        )
        return float(raw)

    async def close(self) -> None:
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from evalharness.worker.concurrency import is_redis_url

        url = _redis_url()
        _limiter = RedisRateLimiter(url) if is_redis_url(url) else MemoryRateLimiter()
        LOGGER.debug(
            "rate limiter for pid=%s is %s (redis_url=%r)",
            os.getpid(),
            type(_limiter).__name__,
            url,
        )
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def _redis_url() -> str:
    try:
        from evalharness.config import get_settings

        return get_settings().redis_url
    except Exception:  # pragma: no cover - settings are always loadable in practice
        return os.environ.get("REDIS_URL", "")


async def acquire_detailed(
    model_key: str,
    rpm: int,
    *,
    limiter: RateLimiter | None = None,
    max_wait_s: float = MAX_WAIT_SECONDS,
    sleep: Any = None,
) -> Acquisition:
    """Wait until this model may make another request.

    A limiter failure never blocks the call: if Redis is unreachable the request
    goes out unshaped and the provider's Retry-After backoff deals with any 429.
    Refusing to run because the throttle is down would be worse than running
    slightly too fast -- but the bypass is counted (D31), never merely logged.
    """
    limiter = limiter or get_rate_limiter()
    sleeper = sleep or asyncio.sleep
    waited = 0.0
    while True:
        try:
            wait = await limiter.take(model_key, rpm)
        except Exception as exc:
            LOGGER.warning("rate limiter unavailable for %s: %s", model_key, exc)
            return Acquisition(waited, bypassed=True)

        # Debug-level so a run can prove every worker process shared one window
        # (D32): the same key appearing under several pids is the evidence.
        LOGGER.debug(
            "ratelimit acquire key=%s pid=%s rpm=%s wait=%.3f waited=%.3f",
            WINDOW_KEY.format(model_key=model_key),
            os.getpid(),
            rpm,
            wait,
            waited,
        )

        if wait <= 0:
            return Acquisition(waited)
        if waited >= max_wait_s:
            LOGGER.warning(
                "waited %.1fs for a %s slot and gave up; letting the call through",
                waited,
                model_key,
            )
            return Acquisition(waited, bypassed=True)
        pause = max(0.01, min(wait, POLL_SECONDS, max_wait_s - waited))
        await sleeper(pause)
        waited += pause


async def acquire(model_key: str, rpm: int, **kwargs: Any) -> float:
    """Seconds waited for a slot. For callers that ignore bypasses."""
    return (await acquire_detailed(model_key, rpm, **kwargs)).waited_s
