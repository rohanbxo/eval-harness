"""Persistence: SQLAlchemy models, session plumbing and the repository layer."""

from evalharness.db import models, repository
from evalharness.db.models import (
    AssertionResult,
    Attempt,
    Base,
    Event,
    Run,
    Turn,
)
from evalharness.db.session import (
    Database,
    close_database,
    create_database,
    database_scope,
    get_database,
    normalize_database_url,
    resolve_database_url,
    set_database,
)

__all__ = [
    "AssertionResult",
    "Attempt",
    "Base",
    "Database",
    "Event",
    "Run",
    "Turn",
    "close_database",
    "create_database",
    "database_scope",
    "get_database",
    "models",
    "normalize_database_url",
    "repository",
    "resolve_database_url",
    "set_database",
]
