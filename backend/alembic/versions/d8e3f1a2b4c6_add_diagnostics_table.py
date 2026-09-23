"""add diagnostics table

Revision ID: d8e3f1a2b4c6
Revises: c7d1e2f3a4b5
Create Date: 2026-05-24 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d8e3f1a2b4c6"
down_revision: str | Sequence[str] | None = "c7d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diagnostics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_diagnostics_acct_occurred",
        "diagnostics",
        ["account_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_diagnostics_acct_occurred", table_name="diagnostics")
    op.drop_table("diagnostics")
