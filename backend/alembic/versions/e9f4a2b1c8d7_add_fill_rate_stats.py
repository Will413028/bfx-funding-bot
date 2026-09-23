"""add fill_rate_stats

Revision ID: e9f4a2b1c8d7
Revises: d8e3f1a2b4c6
Create Date: 2026-05-26 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e9f4a2b1c8d7"
down_revision: str | Sequence[str] | None = "d8e3f1a2b4c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fill_rate_stats",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("period_agg", sa.Text(), nullable=False),
        sa.Column("horizon_h", sa.Integer(), nullable=False),
        sa.Column("spread_bucket_bps", sa.Integer(), nullable=False),
        sa.Column("fill_prob", sa.Float(), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("ttf_p50_ms", sa.BigInteger(), nullable=True),
        sa.Column("ttf_p90_ms", sa.BigInteger(), nullable=True),
        sa.Column("mean_ttf_ms", sa.BigInteger(), nullable=True),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_range_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("candle_range_end_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source", "symbol", "period_agg", "horizon_h", "spread_bucket_bps"
        ),
    )


def downgrade() -> None:
    op.drop_table("fill_rate_stats")
