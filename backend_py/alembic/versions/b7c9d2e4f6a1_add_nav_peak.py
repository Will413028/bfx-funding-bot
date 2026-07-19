"""add nav_peak (per-symbol all-time NAV high-water mark)

Revision ID: b7c9d2e4f6a1
Revises: 5072ec664a0e
Create Date: 2026-07-19 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c9d2e4f6a1"
down_revision: str | Sequence[str] | None = "5072ec664a0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nav_peak",
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("peak", sa.Numeric(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", "deployment_environment", "symbol"),
    )


def downgrade() -> None:
    op.drop_table("nav_peak")
