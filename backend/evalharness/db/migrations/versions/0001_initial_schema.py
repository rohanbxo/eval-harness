"""initial schema: runs, attempts, turns, events, assertion_results

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-10

Mirrors SPEC 8.3. jsonb on Postgres, plain JSON on other dialects so the same
migration runs against the SQLite test database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _big_int() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


RUN_STATUS = sa.Enum(
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    name="run_status",
    native_enum=False,
    length=16,
)
ATTEMPT_STATUS = sa.Enum(
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    name="attempt_status",
    native_enum=False,
    length=16,
)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", RUN_STATUS, nullable=False),
        sa.Column("model_key", sa.String(length=128), nullable=False),
        sa.Column("litellm_model", sa.String(length=256), nullable=False),
        sa.Column("params", _json(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("scenario_ids", _json(), nullable=False),
        sa.Column("config_hashes", _json(), nullable=False),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("harness_version", sa.String(length=32), nullable=False),
        sa.Column("scenario_snapshots", _json(), nullable=False),
        sa.Column("summary", _json(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runs_model_key_status_created_at",
        "runs",
        ["model_key", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=128), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("status", ATTEMPT_STATUS, nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("critical_failure", sa.Boolean(), nullable=False),
        sa.Column("axis_scores", _json(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "scenario_id", "repetition", name="uq_attempts_identity"),
    )
    op.create_index("ix_attempts_run_id", "attempts", ["run_id"], unique=False)

    op.create_table(
        "turns",
        sa.Column("id", _big_int(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("final_response", sa.Text(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("limit_exceeded", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "index", name="uq_turns_attempt_index"),
    )
    op.create_index("ix_turns_attempt_id", "turns", ["attempt_id"], unique=False)

    # Append-only audit trail (SPEC 8.3): rows are inserted and never modified.
    op.create_table(
        "events",
        sa.Column("id", _big_int(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_attempt_id_seq", "events", ["attempt_id", "seq"], unique=False)

    op.create_table(
        "assertion_results",
        sa.Column("id", _big_int(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=True),
        sa.Column("assertion_id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("axis", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", _json(), nullable=False),
        sa.Column("non_deterministic", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assertion_results_attempt_id", "assertion_results", ["attempt_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_assertion_results_attempt_id", table_name="assertion_results")
    op.drop_table("assertion_results")
    op.drop_index("ix_events_attempt_id_seq", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_turns_attempt_id", table_name="turns")
    op.drop_table("turns")
    op.drop_index("ix_attempts_run_id", table_name="attempts")
    op.drop_table("attempts")
    op.drop_index("ix_runs_model_key_status_created_at", table_name="runs")
    op.drop_table("runs")
