"""Per-model request-rate limiting (DECISIONS D27).

Providers cap requests per minute *per model*, so the fix for hitting that cap is
to shape the request rate, not to serialize the whole run: four models can run in
parallel provided each stays under its own limit. This is a different constraint
from the provider concurrency semaphore in ``concurrency.py``, which bounds how
many attempts run at once; a single attempt can still exceed an RPM cap on its
own by making ten calls in quick succession.

The bucket lives in Redis so every worker process shares one allowance. Without
Redis it degrades to a process-local bucket, which is correct for a single
worker and merely approximate for several -- the Retry-After backoff in the
provider remains the backstop either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

#: Redis key for one model's bucket.
BUCKET_KEY = "evalharness:rpm:{model_key}"

#: How long a bucket outlives its last use.
BUCKET_TTL_SECONDS = 300

#: Longest a single acquire will wait before giving up and letting the call
#: through. The provider's Retry-After backoff then handles the 429 if one comes.
MAX_WAIT_SECONDS = 120.0

#: Granularity of the wait loop. Small enough to keep throughput near the cap.
POLL_SECONDS = 0.25

#: A token bucket, refilled continuously at rpm/60 per second. Returns the
#: seconds to wait for the next token, or 0 when one was taken.
#:
#: Continuous refill rather than a fixed window, because a window lets a run fire
#: its whole minute's allowance in one burst and trip the provider anyway.
_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local rpm = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local rate = rpm / 60.0

local bucket = redis.call('HMGET', key, 'tokens', 'updated')
local tokens = tonumber(bucket[1])
local updated = tonumber(bucket[2])
if tokens == nil or updated == nil then
  tokens = rpm
  updated = now
end

tokens = math.min(rpm, tokens + (now - updated) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', key, ttl)
return tostring(wait)
"""


class RateLimiter(ABC):
    """Shapes the request rate for one model key."""

    @abstractmethod
    async def take(self, model_key: str, rpm: int) -> float:
        """Take a token, or return the seconds until one is available."""

    async def close(self) -> None:  # pragma: no cover - overridden where needed
        return None


class MemoryRateLimiter(RateLimiter):
    """Process-local bucket. Exact for one worker, approximate for several."""

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

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RedisRateLimiter(RateLimiter):
    """Token bucket in Redis, shared by every worker process."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._clients: dict[int, Any] = {}

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
        raw = await self._client().eval(
            _ACQUIRE_SCRIPT,
            1,
            BUCKET_KEY.format(model_key=model_key),
            rpm,
            time.time(),
            BUCKET_TTL_SECONDS,
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
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def _redis_url() -> str:
    import os

    try:
        from evalharness.config import get_settings

        return get_settings().redis_url
    except Exception:  # pragma: no cover - settings are always loadable in practice
        return os.environ.get("REDIS_URL", "")


@dataclass(frozen=True)
class Acquisition:
    """The outcome of one attempt to take a token."""

    waited_s: float
    bypassed: bool = False
    """True when the call went out unshaped: the limiter failed, or we gave up
    waiting. Counted per run so a quiet degradation is visible (D31)."""


async def acquire_detailed(
    model_key: str,
    rpm: int,
    *,
    limiter: RateLimiter | None = None,
    max_wait_s: float = MAX_WAIT_SECONDS,
    sleep: Any = None,
) -> Acquisition:
    """Wait until this model may make another request. Returns seconds waited.

    A limiter failure never blocks the call: if Redis is unreachable the request
    goes out unshaped and the provider's Retry-After backoff deals with any 429.
    Refusing to run because the throttle is down would be worse than running
    slightly too fast.
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
        if wait <= 0:
            return Acquisition(waited)
        if waited >= max_wait_s:
            LOGGER.warning(
                "waited %.1fs for a %s token and gave up; letting the call through",
                waited,
                model_key,
            )
            return Acquisition(waited, bypassed=True)
        pause = min(wait, POLL_SECONDS, max_wait_s - waited)
        await sleeper(pause)
        waited += pause


async def acquire(model_key: str, rpm: int, **kwargs: Any) -> float:
    """Seconds waited for a token. Thin wrapper for callers that ignore bypasses."""
    return (await acquire_detailed(model_key, rpm, **kwargs)).waited_s
