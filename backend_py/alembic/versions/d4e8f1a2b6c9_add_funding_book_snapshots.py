"""add funding_book_snapshots (self-collected book depth history)

Revision ID: d4e8f1a2b6c9
Revises: b7c9d2e4f6a1
Create Date: 2026-07-19 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "d4e8f1a2b6c9"
down_revision: str | Sequence[str] | None = "b7c9d2e4f6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "funding_book_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("captured_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("best_bid_rate", sa.Numeric(), nullable=True),
        sa.Column("best_ask_rate", sa.Numeric(), nullable=True),
        sa.Column("bid_depth", sa.Numeric(), nullable=False),
        sa.Column("ask_depth", sa.Numeric(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_book_snapshots_symbol_ts",
        "funding_book_snapshots",
        ["symbol", "captured_at_ms"],
    )


def downgrade() -> None:
    op.drop_index("idx_book_snapshots_symbol_ts", table_name="funding_book_snapshots")
    op.drop_table("funding_book_snapshots")
