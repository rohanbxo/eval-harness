"""Async engine and session plumbing.

An async engine is bound to the event loop that created its connections, so it
must never be shared across loops. That is why this module hands out a
``Database`` object rather than a module-level engine: the API creates one in its
lifespan, and every Celery task creates (and disposes) its own inside the loop it
spins up. ``get_database()`` exists only for the long-lived API process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from evalharness.db.models import Base

#: Sync driver prefixes that are silently upgraded to their async equivalents,
#: so a copy-pasted ``postgresql://`` URL does not fail deep inside the driver.
_ASYNC_DRIVERS: dict[str, str] = {
    "postgresql://": "postgresql+asyncpg://",
    "postgres://": "postgresql+asyncpg://",
    "sqlite://": "sqlite+aiosqlite://",
}


def normalize_database_url(url: str) -> str:
    for prefix, replacement in _ASYNC_DRIVERS.items():
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


def resolve_database_url(url: str | None = None) -> str:
    """Explicit URL wins; otherwise fall back to settings."""
    if url:
        return normalize_database_url(url)
    from evalharness.config import get_settings

    return normalize_database_url(get_settings().database_url)


@dataclass(frozen=True)
class Database:
    """An engine plus its session factory, owned by exactly one event loop."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self.session_factory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def create_all(self) -> None:
        """Create the schema directly. Alembic owns this in production; tests use it."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


def create_database(url: str | None = None, *, echo: bool = False) -> Database:
    """Build a ``Database`` for the *current* event loop."""
    resolved = resolve_database_url(url)
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if resolved.startswith("sqlite"):
        # SQLite connections are cheap and must not outlive the loop that made them.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(resolved, **kwargs)
    if resolved.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return Database(engine=engine, session_factory=factory)


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """SQLite ignores FK constraints (and so ON DELETE CASCADE) unless asked."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@asynccontextmanager
async def database_scope(url: str | None = None) -> AsyncIterator[Database]:
    """A short-lived database for one event loop -- the Celery task entry point."""
    database = create_database(url)
    try:
        yield database
    finally:
        await database.dispose()


_database: Database | None = None


def get_database() -> Database:
    """The long-lived API-process database, created on first use."""
    global _database
    if _database is None:
        _database = create_database()
    return _database


def set_database(database: Database | None) -> None:
    """Install a database (the API lifespan and the test fixtures both do this)."""
    global _database
    _database = database


async def close_database() -> None:
    global _database
    if _database is not None:
        await _database.dispose()
        _database = None
