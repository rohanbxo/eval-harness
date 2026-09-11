"""Per-provider concurrency limiting and cancellation (SPEC 9.1).

The semaphore exists to respect provider rate limits across worker processes, so
the properties that matter are: it caps holders, it releases, expired leases do
not leak slots, and — most important — a Redis outage degrades to unlimited
concurrency rather than refusing to evaluate anything.

The Redis-backed paths run against a small fake client, so no server is needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

from evalharness.worker.concurrency import (
    CANCEL_KEY,
    SEMAPHORE_KEY,
    MemoryCancelStore,
    MemoryProviderLimiter,
    RedisCancelStore,
    RedisProviderLimiter,
    default_concurrency,
    get_cancel_store,
    get_limiter,
    is_redis_url,
    provider_of,
    provider_slot,
    set_cancel_store,
    set_limiter,
)

# --------------------------------------------------------------------------- #
# Fake Redis                                                                   #
# --------------------------------------------------------------------------- #


class FakeRedis:
    """Just enough Redis for the semaphore and the cancel flag."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.closed = False

    async def eval(self, _script: str, _numkeys: int, *args: Any) -> int:
        # (key, now, limit, expiry, token, ttl) -- mirrors the acquire script.
        key, now, limit, expiry, token, _ttl = args
        members = self.zsets.setdefault(str(key), {})
        for member, score in list(members.items()):
            if score <= float(now):
                del members[member]
        if len(members) >= int(limit):
            return 0
        members[str(token)] = float(expiry)
        return 1

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def zremrangebyscore(self, key: str, _min: Any, maximum: Any) -> int:
        members = self.zsets.setdefault(key, {})
        doomed = [m for m, score in members.items() if score <= float(maximum)]
        for member in doomed:
            del members[member]
        return len(doomed)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.strings.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        self.closed = True


class BrokenRedis(FakeRedis):
    """A client whose every operation fails, standing in for an outage."""

    async def eval(self, *_args: Any, **_kwargs: Any) -> int:
        raise ConnectionError("redis is down")

    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis is down")


def redis_limiter(client: FakeRedis) -> RedisProviderLimiter:
    limiter = RedisProviderLimiter("redis://localhost:6379/0")
    limiter._clients[id(asyncio.get_running_loop())] = client
    return limiter


def redis_cancel_store(client: FakeRedis) -> RedisCancelStore:
    store = RedisCancelStore("redis://localhost:6379/0")
    store._clients[id(asyncio.get_running_loop())] = client
    return store


@pytest.fixture(autouse=True)
def _isolate_globals() -> Iterator[None]:
    """Never let one test's limiter leak into the next."""
    set_limiter(None)
    set_cancel_store(None)
    yield
    set_limiter(None)
    set_cancel_store(None)


# --------------------------------------------------------------------------- #
# Provider naming -- the rate-limit bucket                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-opus-5", "anthropic"),
        ("openai/gpt-4o", "openai"),
        ("fake/transcript", "fake"),
        ("ollama/llama3", "ollama"),
        ("bare-name", "bare-name"),
    ],
)
def test_provider_of_buckets_by_vendor(model: str, expected: str) -> None:
    assert provider_of(model) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://localhost:6379/0", True),
        ("rediss://host/0", True),
        ("unix:///var/run/redis.sock", True),
        ("memory://", False),
        ("", False),
        (None, False),
    ],
)
def test_only_real_redis_urls_select_the_redis_backend(url: str | None, expected: bool) -> None:
    """`memory://` is a supported local setup, not a misconfiguration."""
    assert is_redis_url(url) is expected


def test_memory_backends_are_chosen_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "memory://")
    from evalharness.config import get_settings

    get_settings.cache_clear()
    try:
        assert isinstance(get_limiter(), MemoryProviderLimiter)
        assert isinstance(get_cancel_store(), MemoryCancelStore)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The in-process limiter                                                       #
# --------------------------------------------------------------------------- #


async def test_limit_is_enforced() -> None:
    limiter = MemoryProviderLimiter()
    first = await limiter.try_acquire("openai", 2, 60)
    second = await limiter.try_acquire("openai", 2, 60)
    third = await limiter.try_acquire("openai", 2, 60)

    assert first is not None
    assert second is not None
    assert third is None, "a third holder must not get in under a limit of 2"
    assert await limiter.holders("openai") == 2


async def test_releasing_frees_a_slot() -> None:
    limiter = MemoryProviderLimiter()
    token = await limiter.try_acquire("openai", 1, 60)
    assert token is not None
    assert await limiter.try_acquire("openai", 1, 60) is None

    await limiter.release("openai", token)
    assert await limiter.try_acquire("openai", 1, 60) is not None


async def test_providers_are_limited_independently() -> None:
    """A busy vendor must not block a different one."""
    limiter = MemoryProviderLimiter()
    assert await limiter.try_acquire("openai", 1, 60) is not None
    assert await limiter.try_acquire("openai", 1, 60) is None
    assert await limiter.try_acquire("anthropic", 1, 60) is not None


async def test_an_expired_lease_does_not_leak_a_slot() -> None:
    """A worker that dies holding a slot must not wedge the provider forever."""
    limiter = MemoryProviderLimiter()
    assert await limiter.try_acquire("openai", 1, lease_s=0) is not None
    await asyncio.sleep(0.01)
    assert await limiter.try_acquire("openai", 1, 60) is not None
    assert await limiter.holders("openai") == 1


async def test_releasing_an_unknown_token_is_harmless() -> None:
    await MemoryProviderLimiter().release("openai", "never-issued")


async def test_holders_ignores_expired_leases() -> None:
    limiter = MemoryProviderLimiter()
    await limiter.try_acquire("openai", 5, lease_s=0)
    await asyncio.sleep(0.01)
    assert await limiter.holders("openai") == 0


# --------------------------------------------------------------------------- #
# The Redis limiter                                                            #
# --------------------------------------------------------------------------- #


async def test_redis_limiter_caps_holders() -> None:
    client = FakeRedis()
    limiter = redis_limiter(client)

    assert await limiter.try_acquire("openai", 2, 60) is not None
    assert await limiter.try_acquire("openai", 2, 60) is not None
    assert await limiter.try_acquire("openai", 2, 60) is None
    assert await limiter.holders("openai") == 2
    assert SEMAPHORE_KEY.format(provider="openai") in client.zsets


async def test_redis_limiter_releases() -> None:
    client = FakeRedis()
    limiter = redis_limiter(client)
    token = await limiter.try_acquire("openai", 1, 60)
    assert token is not None

    await limiter.release("openai", token)
    assert await limiter.holders("openai") == 0
    assert await limiter.try_acquire("openai", 1, 60) is not None


async def test_redis_limiter_release_survives_an_outage() -> None:
    """Releasing is best-effort: the lease expires on its own anyway."""
    limiter = redis_limiter(BrokenRedis())
    await limiter.release("openai", "token")  # must not raise


async def test_redis_limiter_closes_its_clients() -> None:
    client = FakeRedis()
    limiter = redis_limiter(client)
    await limiter.close()
    assert client.closed is True


# --------------------------------------------------------------------------- #
# provider_slot                                                                #
# --------------------------------------------------------------------------- #


async def test_slot_is_granted_and_returned() -> None:
    limiter = MemoryProviderLimiter()
    async with provider_slot("openai", limit=1, limiter=limiter) as granted:
        assert granted is True
        assert await limiter.holders("openai") == 1
    assert await limiter.holders("openai") == 0


async def test_slot_is_returned_even_when_the_body_raises() -> None:
    limiter = MemoryProviderLimiter()
    with pytest.raises(RuntimeError):
        async with provider_slot("openai", limit=1, limiter=limiter):
            raise RuntimeError("attempt blew up")
    assert await limiter.holders("openai") == 0, "a crashed attempt must not hold its slot"


async def test_a_redis_outage_degrades_to_unlimited_rather_than_blocking() -> None:
    """Refusing to evaluate anything would be far worse than ignoring the limit."""

    class DownLimiter(MemoryProviderLimiter):
        async def try_acquire(self, provider: str, limit: int, lease_s: int) -> str | None:
            raise ConnectionError("redis is down")

    async with provider_slot("openai", limit=1, limiter=DownLimiter()) as granted:
        assert granted is False  # ran anyway, just without a slot


async def test_waiting_times_out_when_the_provider_stays_full() -> None:
    limiter = MemoryProviderLimiter()
    held = await limiter.try_acquire("openai", 1, 60)
    assert held is not None

    with pytest.raises(TimeoutError, match="openai"):
        async with provider_slot("openai", limit=1, limiter=limiter, wait_timeout_s=0.05):
            pass  # pragma: no cover - the slot is never granted


async def test_a_waiter_proceeds_once_a_slot_frees_up() -> None:
    limiter = MemoryProviderLimiter()
    token = await limiter.try_acquire("openai", 1, 60)
    assert token is not None

    async def release_shortly() -> None:
        await asyncio.sleep(0.05)
        await limiter.release("openai", token)

    releaser = asyncio.create_task(release_shortly())
    async with provider_slot("openai", limit=1, limiter=limiter, wait_timeout_s=2.0) as granted:
        assert granted is True
    await releaser


async def test_slot_defaults_to_the_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALHARNESS_PROVIDER_CONCURRENCY", "2")
    from evalharness.config import get_settings

    get_settings.cache_clear()
    try:
        assert default_concurrency() == 2
        limiter = MemoryProviderLimiter()
        async with (
            provider_slot("openai", limiter=limiter),
            provider_slot("openai", limiter=limiter),
        ):
            assert await limiter.holders("openai") == 2
            assert await limiter.try_acquire("openai", 2, 60) is None
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Cancellation                                                                 #
# --------------------------------------------------------------------------- #


async def test_memory_cancel_store_round_trip() -> None:
    store = MemoryCancelStore()
    assert await store.is_cancelled("run-1") is False

    await store.mark_cancelled("run-1")
    assert await store.is_cancelled("run-1") is True
    assert await store.is_cancelled("run-2") is False

    await store.clear("run-1")
    assert await store.is_cancelled("run-1") is False


async def test_clearing_an_uncancelled_run_is_harmless() -> None:
    await MemoryCancelStore().clear("never-seen")


async def test_redis_cancel_store_round_trip() -> None:
    client = FakeRedis()
    store = redis_cancel_store(client)

    assert await store.is_cancelled("run-1") is False
    await store.mark_cancelled("run-1")
    assert client.strings[CANCEL_KEY.format(run_id="run-1")] == "1"
    assert await store.is_cancelled("run-1") is True

    await store.clear("run-1")
    assert await store.is_cancelled("run-1") is False


async def test_a_cancel_lookup_outage_reports_not_cancelled() -> None:
    """Failing open matters: a Redis blip must not silently abort every attempt."""
    store = redis_cancel_store(BrokenRedis())
    assert await store.is_cancelled("run-1") is False


async def test_clearing_survives_an_outage() -> None:
    await redis_cancel_store(BrokenRedis()).clear("run-1")  # must not raise


def test_installed_limiter_and_store_are_returned() -> None:
    limiter = MemoryProviderLimiter()
    store = MemoryCancelStore()
    set_limiter(limiter)
    set_cancel_store(store)
    assert get_limiter() is limiter
    assert get_cancel_store() is store


async def test_leases_expire_so_a_dead_worker_frees_its_slot_in_redis() -> None:
    client = FakeRedis()
    limiter = redis_limiter(client)
    key = SEMAPHORE_KEY.format(provider="openai")

    assert await limiter.try_acquire("openai", 1, 60) is not None
    # Simulate the holder's lease having elapsed.
    client.zsets[key] = {token: time.time() - 1 for token in client.zsets[key]}
    assert await limiter.holders("openai") == 0
    assert await limiter.try_acquire("openai", 1, 60) is not None
