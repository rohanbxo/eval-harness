"""Per-provider rate limiting and run cancellation (SPEC 9.1).

Two pieces of cross-process coordination live here, both Redis-backed with an
in-process fallback so tests (and a Redis-less CLI run) still exercise the code:

**Provider semaphore.** At most ``provider_concurrency`` attempts run against one
provider at a time, so a k=5 x 5-scenario fan-out cannot walk into a 429. It is a
*leased* semaphore: a holder is a member of a sorted set scored with its expiry,
and every acquire first drops expired members. A worker killed mid-attempt
therefore releases its slot on its own after the lease, instead of wedging the
provider forever the way a plain token list would.

**Cancellation flag.** ``POST /runs/{id}/cancel`` sets a key here (and the run's
status in Postgres). Running attempts check it between model calls through
``AttemptContext.cancel_check``; queued attempts check it before they start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

LOGGER = logging.getLogger(__name__)

SEMAPHORE_KEY = "evalharness:semaphore:{provider}"
CANCEL_KEY = "evalharness:cancel:{run_id}"

#: How long a single attempt may hold a provider slot before the lease is
#: reclaimed. Longer than any realistic attempt; short enough to self-heal.
DEFAULT_LEASE_SECONDS = 3600
#: How long a cancellation flag lingers, so a late-starting attempt still sees it.
CANCEL_TTL_SECONDS = 24 * 3600
#: Polling interval while waiting for a free slot.
POLL_SECONDS = 0.25

_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


def provider_of(litellm_model: str) -> str:
    """``anthropic/claude-opus-5`` -> ``anthropic``; unqualified names map to themselves."""
    return litellm_model.split("/", 1)[0] if "/" in litellm_model else litellm_model


class ProviderLimiter(ABC):
    """A leased, per-provider semaphore."""

    @abstractmethod
    async def try_acquire(self, provider: str, limit: int, lease_s: int) -> str | None:
        """Return a holder token, or None when the provider is at its limit."""

    @abstractmethod
    async def release(self, provider: str, token: str) -> None: ...

    @abstractmethod
    async def holders(self, provider: str) -> int: ...


class MemoryProviderLimiter(ProviderLimiter):
    """Process-local fallback. Thread-safe, because Celery tasks bring their own loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, dict[str, float]] = {}

    async def try_acquire(self, provider: str, limit: int, lease_s: int) -> str | None:
        now = time.time()
        with self._lock:
            held = self._held.setdefault(provider, {})
            for token, expiry in list(held.items()):
                if expiry <= now:
                    del held[token]
            if len(held) >= limit:
                return None
            token = uuid.uuid4().hex
            held[token] = now + lease_s
            return token

    async def release(self, provider: str, token: str) -> None:
        with self._lock:
            self._held.get(provider, {}).pop(token, None)

    async def holders(self, provider: str) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for expiry in self._held.get(provider, {}).values() if expiry > now)


class RedisProviderLimiter(ProviderLimiter):
    """Redis sorted-set semaphore shared by every worker process."""

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

    async def try_acquire(self, provider: str, limit: int, lease_s: int) -> str | None:
        token = uuid.uuid4().hex
        now = time.time()
        key = SEMAPHORE_KEY.format(provider=provider)
        granted = await self._client().eval(
            _ACQUIRE_SCRIPT, 1, key, now, limit, now + lease_s, token, lease_s * 2
        )
        return token if int(granted) == 1 else None

    async def release(self, provider: str, token: str) -> None:
        with contextlib.suppress(Exception):
            await self._client().zrem(SEMAPHORE_KEY.format(provider=provider), token)

    async def holders(self, provider: str) -> int:
        key = SEMAPHORE_KEY.format(provider=provider)
        await self._client().zremrangebyscore(key, "-inf", time.time())
        return int(await self._client().zcard(key))

    async def close(self) -> None:
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()


class CancelStore(ABC):
    """Where a cancellation request is visible to every worker."""

    @abstractmethod
    async def mark_cancelled(self, run_id: str) -> None: ...

    @abstractmethod
    async def is_cancelled(self, run_id: str) -> bool: ...

    @abstractmethod
    async def clear(self, run_id: str) -> None: ...


class MemoryCancelStore(CancelStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()

    async def mark_cancelled(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.add(run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    async def clear(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.discard(run_id)


class RedisCancelStore(CancelStore):
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

    async def mark_cancelled(self, run_id: str) -> None:
        await self._client().set(CANCEL_KEY.format(run_id=run_id), "1", ex=CANCEL_TTL_SECONDS)

    async def is_cancelled(self, run_id: str) -> bool:
        try:
            return bool(await self._client().get(CANCEL_KEY.format(run_id=run_id)))
        except Exception as exc:
            LOGGER.warning("cancel lookup failed for run %s: %s", run_id, exc)
            return False

    async def clear(self, run_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._client().delete(CANCEL_KEY.format(run_id=run_id))


def _redis_url() -> str:
    try:
        from evalharness.config import get_settings

        return get_settings().redis_url
    except Exception as exc:
        LOGGER.warning("no Redis configured (%s); coordinating in-process only", exc)
        return ""


_limiter: ProviderLimiter | None = None
_cancel_store: CancelStore | None = None


#: Schemes the redis client can actually dial. Anything else (notably the
#: ``memory://`` broker used for local runs and tests) means "no Redis here",
#: which is a supported configuration rather than an error worth logging.
REDIS_SCHEMES: tuple[str, ...] = ("redis://", "rediss://", "unix://")


def is_redis_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(REDIS_SCHEMES)


def get_limiter() -> ProviderLimiter:
    global _limiter
    if _limiter is None:
        url = _redis_url()
        _limiter = RedisProviderLimiter(url) if is_redis_url(url) else MemoryProviderLimiter()
    return _limiter


def set_limiter(limiter: ProviderLimiter | None) -> None:
    global _limiter
    _limiter = limiter


def get_cancel_store() -> CancelStore:
    global _cancel_store
    if _cancel_store is None:
        url = _redis_url()
        _cancel_store = RedisCancelStore(url) if is_redis_url(url) else MemoryCancelStore()
    return _cancel_store


def set_cancel_store(store: CancelStore | None) -> None:
    global _cancel_store
    _cancel_store = store


def default_concurrency() -> int:
    try:
        from evalharness.config import get_settings

        return int(get_settings().provider_concurrency)
    except Exception:
        return 4


@asynccontextmanager
async def provider_slot(
    provider: str,
    *,
    limit: int | None = None,
    lease_s: int = DEFAULT_LEASE_SECONDS,
    wait_timeout_s: float | None = None,
    limiter: ProviderLimiter | None = None,
) -> AsyncIterator[bool]:
    """Hold one of ``provider``'s slots for the duration of the block.

    Yields True when a slot was granted. If Redis is unreachable the block still
    runs (yielding False): degrading to unlimited concurrency is much better than
    refusing to evaluate anything.
    """
    limiter = limiter or get_limiter()
    limit = limit if limit is not None else default_concurrency()
    deadline = None if wait_timeout_s is None else time.monotonic() + wait_timeout_s
    token: str | None = None

    while True:
        try:
            token = await limiter.try_acquire(provider, limit, lease_s)
        except Exception as exc:
            LOGGER.warning("provider semaphore unavailable for %s: %s", provider, exc)
            token = None
            break
        if token is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for a {provider} slot (limit {limit})")
        await asyncio.sleep(POLL_SECONDS)

    try:
        yield token is not None
    finally:
        if token is not None:
            with contextlib.suppress(Exception):
                await limiter.release(provider, token)
