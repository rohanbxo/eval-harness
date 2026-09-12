"""SQLAlchemy models for everything a run produces (SPEC 8.3).

Scenario *definitions* live in git; only run output lives here. ``events`` and
``assertion_results`` are append-only -- together they are the audit trail, and
the repository layer deliberately exposes no update or delete for them.

Postgres is the production database, but the test suite runs on SQLite, so every
jsonb column is declared dialect-neutrally as ``JSON().with_variant(JSONB, ...)``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeEngine

from evalharness.schema.enums import AttemptStatus, RunStatus

JsonDict = dict[str, Any]


def json_column() -> TypeEngine[Any]:
    """jsonb on Postgres, plain JSON everywhere else (the test DB is SQLite)."""
    return JSON().with_variant(JSONB, "postgresql")


def big_int_pk() -> TypeEngine[Any]:
    """SQLite only auto-increments ``INTEGER PRIMARY KEY``, never ``BIGINT``."""
    return BigInteger().with_variant(Integer, "sqlite")


def status_enum(enum_cls: type[RunStatus] | type[AttemptStatus], name: str) -> SAEnum:
    """A VARCHAR + CHECK constraint storing the enum's *values*, not its member names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base; Alembic autogenerate reads ``Base.metadata``."""


class Run(Base):
    """One model x scenario-set x k execution (SPEC 8.3)."""

    __tablename__ = "runs"
    __table_args__ = (
        # The leaderboard filters by model + status and takes the newest run.
        Index("ix_runs_model_key_status_created_at", "model_key", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        status_enum(RunStatus, "run_status"), default=RunStatus.QUEUED, nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    litellm_model: Mapped[str] = mapped_column(String(256), nullable=False)
    params: Mapped[JsonDict] = mapped_column(json_column(), default=dict, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    scenario_ids: Mapped[list[str]] = mapped_column(json_column(), default=list, nullable=False)
    config_hashes: Mapped[dict[str, str]] = mapped_column(
        json_column(), default=dict, nullable=False
    )
    git_commit: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    harness_version: Mapped[str] = mapped_column(String(32), default="0.0.0", nullable=False)
    scenario_snapshots: Mapped[JsonDict] = mapped_column(
        json_column(), default=dict, nullable=False
    )
    summary: Mapped[JsonDict | None] = mapped_column(json_column(), nullable=True)

    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class Attempt(Base):
    """One scenario x repetition within a run."""

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "scenario_id", "repetition", name="uq_attempts_identity"),
        Index("ix_attempts_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(128), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AttemptStatus] = mapped_column(
        status_enum(AttemptStatus, "attempt_status"), default=AttemptStatus.QUEUED, nullable=False
    )
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    critical_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    axis_scores: Mapped[dict[str, float]] = mapped_column(
        json_column(), default=dict, nullable=False
    )
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exposed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the attempt met the scenario's exposure condition, if it has one.

    research-injection can only test resistance to an injected instruction in an
    attempt that actually fetched the page carrying it; one that stopped earlier
    is censored, not a clean negative (D38).
    """

    run: Mapped[Run] = relationship(back_populates="attempts")


class Turn(Base):
    """One scripted user turn inside an attempt."""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("attempt_id", "index", name="uq_turns_attempt_index"),
        Index("ix_turns_attempt_id", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(big_int_pk(), primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    final_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    limit_exceeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Event(Base):
    """One ordered audit-trail entry. APPEND-ONLY: never updated, never deleted."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_attempt_id_seq", "attempt_id", "seq"),)

    id: Mapped[int] = mapped_column(big_int_pk(), primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(json_column(), default=dict, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AssertionResult(Base):
    """One graded assertion. APPEND-ONLY: never updated, never deleted."""

    __tablename__ = "assertion_results"
    __table_args__ = (Index("ix_assertion_results_attempt_id", "attempt_id"),)

    id: Mapped[int] = mapped_column(big_int_pk(), primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assertion_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    axis: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details: Mapped[JsonDict] = mapped_column(json_column(), default=dict, nullable=False)
    non_deterministic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evaluable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    """False when the attempt produced no evidence either way (D38)."""
