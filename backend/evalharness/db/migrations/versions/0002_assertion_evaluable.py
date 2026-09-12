"""assertion_results.evaluable, attempts.exposed

Revision ID: 0002_assertion_evaluable
Revises: 0001_initial_schema
Create Date: 2026-09-12

`evaluable` marks an assertion the attempt produced no evidence for either way
-- a constraint on a tool's arguments where the tool was never called. It
defaults to true so every existing row keeps the meaning it was written with
(DECISIONS D38).

`exposed` records whether the attempt actually met the condition a scenario
says its safety assertions depend on: for research-injection, whether the page
carrying the injected instruction was fetched at all. Null means the scenario
declares no such probe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_assertion_evaluable"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assertion_results",
        sa.Column("evaluable", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("attempts", sa.Column("exposed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "exposed")
    op.drop_column("assertion_results", "evaluable")
