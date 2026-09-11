"""Run-progress fan-out: Redis pub/sub in production, in-process for tests.

The worker publishes progress; the API's SSE endpoint subscribes and relays it to
the browser (SPEC 9.1, 9.2). Two details drive the design here:

* An async Redis client is bound to the event loop that created it, and Celery
  tasks spin up a fresh loop per task, so clients are cached per loop.
* Publishing must never be able to fail a run. Every Redis error is logged and
  swallowed; the SSE stream independently polls the database for run status, so
  a dropped message delays the UI rather than hanging it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from evalharness.api.schemas import ProgressEvent
from evalharness.worker.concurrency import is_redis_url

LOGGER = logging.getLogger(__name__)

#: Redis pub/sub channel for one run's progress.
CHANNEL_TEMPLATE = "evalharness:progress:{run_id}"

#: How long a subscriber waits before emitting an idle tick (seconds).
IDLE_TICK_SECONDS = 1.0


def now() -> datetime:
    return datetime.now(UTC)


class EventBus(ABC):
    """Publish/subscribe for :class:`ProgressEvent`."""

    @abstractmethod
    async def publish(self, event: ProgressEvent) -> None: ...

    @abstractmethod
    def history(self, run_id: str) -> list[ProgressEvent]:
        """Events already published for this run, when the backend keeps any."""

    @abstractmethod
    def subscribe(self, run_id: str, *, skip: int = 0) -> AsyncIterator[ProgressEvent | None]:
        """Yield live events; yield ``None`` on an idle tick so callers can poll."""

    async def close(self) -> None:  # pragma: no cover - overridden where needed
        return None


class MemoryEventBus(EventBus):
    """In-process bus with replayable history.

    Deliberately built on a lock-protected list rather than ``asyncio.Queue``:
    under ``task_always_eager`` the publisher runs in a worker thread with its own
    event loop while the subscriber sits in the API's loop, and an asyncio queue
    would refuse the cross-loop hand-off.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[ProgressEvent]] = {}

    async def publish(self, event: ProgressEvent) -> None:
        with self._lock:
            self._events.setdefault(event.run_id, []).append(event)

    def history(self, run_id: str) -> list[ProgressEvent]:
        with self._lock:
            return list(self._events.get(run_id, ()))

    async def subscribe(self, run_id: str, *, skip: int = 0) -> AsyncIterator[ProgressEvent | None]:
        index = skip
        while True:
            with self._lock:
                pending = self._events.get(run_id, [])[index:]
            if pending:
                index += len(pending)
                for event in pending:
                    yield event
                continue
            await asyncio.sleep(min(IDLE_TICK_SECONDS, 0.1))
            yield None

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


class RedisEventBus(EventBus):
    """Redis pub/sub, with one client per event loop."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._clients: dict[int, Any] = {}

    def _client(self) -> Any:
        import redis.asyncio as redis  # imported lazily: the CLI never needs Redis

        loop_id = id(asyncio.get_running_loop())
        client = self._clients.get(loop_id)
        if client is None:
            client = redis.from_url(self.url, decode_responses=True)  # type: ignore[no-untyped-call]
            self._clients[loop_id] = client
        return client

    async def publish(self, event: ProgressEvent) -> None:
        channel = CHANNEL_TEMPLATE.format(run_id=event.run_id)
        try:
            await self._client().publish(channel, event.model_dump_json())
        except Exception as exc:
            LOGGER.warning("progress publish failed for run %s: %s", event.run_id, exc)

    def history(self, run_id: str) -> list[ProgressEvent]:
        return []  # pub/sub has no backlog; the SSE snapshot covers the gap

    async def subscribe(self, run_id: str, *, skip: int = 0) -> AsyncIterator[ProgressEvent | None]:
        channel = CHANNEL_TEMPLATE.format(run_id=run_id)
        pubsub = self._client().pubsub()
        try:
            await pubsub.subscribe(channel)
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=IDLE_TICK_SECONDS
                )
                if message is None:
                    yield None
                    continue
                yield _parse(message.get("data"))
        except Exception as exc:
            LOGGER.warning("progress subscribe failed for run %s: %s", run_id, exc)
            while True:
                await asyncio.sleep(IDLE_TICK_SECONDS)
                yield None
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def close(self) -> None:
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()


def _parse(data: Any) -> ProgressEvent | None:
    if not isinstance(data, str | bytes):
        return None
    try:
        return ProgressEvent.model_validate(json.loads(data))
    except (ValueError, TypeError) as exc:
        LOGGER.warning("dropping malformed progress event: %s", exc)
        return None


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """The process-wide bus: Redis when configured, in-process otherwise."""
    global _bus
    if _bus is None:
        _bus = _build_bus()
    return _bus


def _build_bus() -> EventBus:
    try:
        from evalharness.config import get_settings

        url = get_settings().redis_url
    except Exception as exc:
        LOGGER.warning("no Redis configured (%s); progress stays in-process", exc)
        return MemoryEventBus()
    # A non-Redis broker (``memory://`` locally and in tests) is a supported
    # setup, not a misconfiguration: progress simply stays in-process.
    return RedisEventBus(url) if is_redis_url(url) else MemoryEventBus()


def set_event_bus(bus: EventBus | None) -> None:
    """Install a bus (the API lifespan and the test fixtures both do this)."""
    global _bus
    _bus = bus


async def publish_progress(event: ProgressEvent) -> None:
    await get_event_bus().publish(event)
