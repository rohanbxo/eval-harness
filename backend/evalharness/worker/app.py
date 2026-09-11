"""Celery application (SPEC 9.1).

Started as ``celery -A evalharness.worker.app worker``, so the module-level name
``app`` is part of the contract with docker-compose.
"""

from __future__ import annotations

import os

from celery import Celery


def _redis_url() -> str:
    try:
        from evalharness.config import get_settings

        return get_settings().redis_url
    except Exception:
        return os.environ.get("REDIS_URL", "memory://")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def create_celery_app() -> Celery:
    broker = _redis_url() or "memory://"
    app = Celery(
        "evalharness",
        broker=broker,
        backend=broker if broker.startswith("redis") else "cache+memory://",
        include=["evalharness.worker.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        # An attempt is long and idempotent-on-retry; late acks keep it from being
        # lost if a worker dies, and prefetch=1 keeps the fan-out evenly spread.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        result_expires=7 * 24 * 3600,
        # Tests flip this on; nothing else should.
        task_always_eager=_truthy(os.environ.get("EVALHARNESS_CELERY_ALWAYS_EAGER")),
        task_eager_propagates=True,
    )
    return app


app = create_celery_app()
#: Alias for code that prefers an explicit name over the Celery convention.
celery_app = app
