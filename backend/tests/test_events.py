"""Progress event bus for SSE (SPEC 9.1, 9.2).

Progress events are a convenience, never the record — the database is. So the
properties worth pinning are that live events reach a subscriber, that a
replaying subscriber can skip what it already sent, and that every Redis failure
degrades to "no live updates" instead of breaking the stream.

The Redis paths run against a small fake client; no server is needed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from evalharness.api.events import (
    CHANNEL_TEMPLATE,
    MemoryEventBus,
    RedisEventBus,
    _parse,
    get_event_bus,
    now,
    publish_progress,
    set_event_bus,
)
from evalharness.api.schemas import ProgressEvent
from evalharness.schema.enums import RunStatus


def event(run_id: str = "run-1", type_: str = "attempt_started", **extra: Any) -> ProgressEvent:
    payload: dict[str, Any] = {
        "type": type_,
        "run_id": run_id,
        "ts": now().isoformat(),
        **extra,
    }
    return ProgressEvent.model_validate(payload)


async def take(
    bus: MemoryEventBus | RedisEventBus, run_id: str, count: int, **kwargs: Any
) -> list[ProgressEvent]:
    """Pull `count` non-idle events off a subscription, with a hard timeout."""
    collected: list[ProgressEvent] = []

    async def pump() -> None:
        async for item in bus.subscribe(run_id, **kwargs):
            if item is not None:
                collected.append(item)
                if len(collected) >= count:
                    return

    await asyncio.wait_for(pump(), timeout=5)
    return collected


@pytest.fixture(autouse=True)
def _isolate_bus() -> Iterator[None]:
    set_event_bus(None)
    yield
    set_event_bus(None)


# --------------------------------------------------------------------------- #
# In-process bus                                                               #
# --------------------------------------------------------------------------- #


async def test_published_events_are_replayable() -> None:
    bus = MemoryEventBus()
    await bus.publish(event(type_="run_started"))
    await bus.publish(event(type_="attempt_finished"))

    history = bus.history("run-1")
    assert [item.type for item in history] == ["run_started", "attempt_finished"]


async def test_history_is_per_run() -> None:
    bus = MemoryEventBus()
    await bus.publish(event("run-1"))
    assert bus.history("run-2") == []


async def test_a_subscriber_receives_live_events() -> None:
    bus = MemoryEventBus()
    await bus.publish(event(type_="attempt_started"))
    await bus.publish(event(type_="run_completed"))

    received = await take(bus, "run-1", 2)
    assert [item.type for item in received] == ["attempt_started", "run_completed"]


async def test_skip_lets_a_stream_resume_past_what_it_already_sent() -> None:
    """The SSE route replays history first, then subscribes past it."""
    bus = MemoryEventBus()
    await bus.publish(event(type_="run_started"))
    await bus.publish(event(type_="attempt_started"))
    await bus.publish(event(type_="run_completed"))

    received = await take(bus, "run-1", 1, skip=2)
    assert [item.type for item in received] == ["run_completed"]


async def test_an_idle_subscription_yields_none_so_the_caller_can_poll() -> None:
    bus = MemoryEventBus()
    subscription = bus.subscribe("quiet-run")
    assert await asyncio.wait_for(anext(subscription), timeout=5) is None
    await subscription.aclose()  # type: ignore[attr-defined]


async def test_events_published_after_subscribing_still_arrive() -> None:
    bus = MemoryEventBus()

    async def publish_shortly() -> None:
        await asyncio.sleep(0.05)
        await bus.publish(event(type_="run_completed"))

    publisher = asyncio.create_task(publish_shortly())
    received = await take(bus, "run-1", 1)
    await publisher
    assert received[0].type == "run_completed"


def test_clear_drops_everything() -> None:
    bus = MemoryEventBus()
    asyncio.run(bus.publish(event()))
    bus.clear()
    assert bus.history("run-1") == []


async def test_publish_progress_uses_the_installed_bus() -> None:
    bus = MemoryEventBus()
    set_event_bus(bus)
    assert get_event_bus() is bus

    await publish_progress(event(type_="run_completed"))
    assert [item.type for item in bus.history("run-1")] == ["run_completed"]


# --------------------------------------------------------------------------- #
# Payload parsing                                                              #
# --------------------------------------------------------------------------- #


def test_parse_reads_a_serialized_event() -> None:
    parsed = _parse(event(type_="run_completed", run_status=RunStatus.COMPLETED).model_dump_json())
    assert parsed is not None
    assert parsed.type == "run_completed"
    assert parsed.run_status == RunStatus.COMPLETED


def test_parse_accepts_bytes() -> None:
    parsed = _parse(event().model_dump_json().encode("utf-8"))
    assert parsed is not None
    assert parsed.run_id == "run-1"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        123,
        {"type": "attempt_started"},
        "not json at all",
        json.dumps({"type": "nonsense", "run_id": "r"}),
        json.dumps({"run_id": "r"}),
    ],
)
def test_malformed_payloads_are_dropped_not_raised(payload: Any) -> None:
    """A bad frame must not tear down someone's live view."""
    assert _parse(payload) is None


# --------------------------------------------------------------------------- #
# Redis bus                                                                    #
# --------------------------------------------------------------------------- #


class FakePubSub:
    def __init__(self, messages: list[Any], *, fail: bool = False) -> None:
        self._messages = messages
        self._fail = fail
        self.channels: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        if self._fail:
            raise ConnectionError("redis is down")
        self.channels.append(channel)

    async def get_message(self, **_: Any) -> Any:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, messages: list[Any] | None = None, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self._messages = messages or []
        self._fail = fail
        self.last_pubsub: FakePubSub | None = None
        self.closed = False

    async def publish(self, channel: str, payload: str) -> int:
        if self._fail:
            raise ConnectionError("redis is down")
        self.published.append((channel, payload))
        return 1

    def pubsub(self) -> FakePubSub:
        self.last_pubsub = FakePubSub(self._messages, fail=self._fail)
        return self.last_pubsub

    async def aclose(self) -> None:
        self.closed = True


def redis_bus(client: FakeRedis) -> RedisEventBus:
    bus = RedisEventBus("redis://localhost:6379/0")
    bus._clients[id(asyncio.get_running_loop())] = client
    return bus


async def test_redis_publish_writes_to_the_run_channel() -> None:
    client = FakeRedis()
    await redis_bus(client).publish(event(type_="attempt_finished"))

    channel, payload = client.published[0]
    assert channel == CHANNEL_TEMPLATE.format(run_id="run-1")
    assert json.loads(payload)["type"] == "attempt_finished"


async def test_redis_publish_failure_is_swallowed() -> None:
    """Losing a progress frame must never fail the attempt that emitted it."""
    await redis_bus(FakeRedis(fail=True)).publish(event())  # must not raise


async def test_redis_bus_has_no_replayable_history() -> None:
    """Pub/sub keeps no backlog; the SSE route sends a DB snapshot instead."""
    assert redis_bus(FakeRedis()).history("run-1") == []


async def test_redis_subscribe_yields_parsed_events() -> None:
    payload = event(type_="run_completed").model_dump_json()
    client = FakeRedis([{"data": payload}])
    bus = redis_bus(client)

    received = await take(bus, "run-1", 1)
    assert received[0].type == "run_completed"
    assert client.last_pubsub is not None
    assert client.last_pubsub.channels == [CHANNEL_TEMPLATE.format(run_id="run-1")]


async def test_redis_subscribe_yields_none_while_idle() -> None:
    bus = redis_bus(FakeRedis([]))
    subscription = bus.subscribe("run-1")
    assert await asyncio.wait_for(anext(subscription), timeout=5) is None
    await subscription.aclose()  # type: ignore[attr-defined]


async def test_a_malformed_frame_yields_none_rather_than_breaking_the_stream() -> None:
    client = FakeRedis([{"data": "garbage"}])
    subscription = redis_bus(client).subscribe("run-1")
    assert await asyncio.wait_for(anext(subscription), timeout=5) is None
    await subscription.aclose()  # type: ignore[attr-defined]


async def test_a_subscribe_outage_degrades_to_idle_ticks() -> None:
    """If Redis is unreachable the stream keeps ticking; the route falls back to polling."""
    subscription = redis_bus(FakeRedis(fail=True)).subscribe("run-1")
    assert await asyncio.wait_for(anext(subscription), timeout=5) is None
    await subscription.aclose()  # type: ignore[attr-defined]


async def test_redis_bus_closes_its_clients() -> None:
    client = FakeRedis()
    bus = redis_bus(client)
    await bus.close()
    assert client.closed is True
