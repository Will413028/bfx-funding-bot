"""add external_signals tables (perp_funding_rates, liquidations)

Research-only inputs for the strategy-research EDA funnel; not read by the
live trading path. Populated by scripts/ingest_perp_funding.py and
scripts/ingest_liquidations.py.

Revision ID: f3b8d2c7a1e4
Revises: d4e8f1a2b6c9
Create Date: 2026-07-19 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3b8d2c7a1e4"
down_revision: str | Sequence[str] | None = "d4e8f1a2b6c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "perp_funding_rates",
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("mts", sa.BigInteger(), nullable=False),
        sa.Column("funding_rate", sa.Float(), nullable=True),
        sa.Column("next_funding_accrued", sa.Float(), nullable=True),
        sa.Column("next_funding_evt_mts", sa.BigInteger(), nullable=True),
        sa.Column("deriv_price", sa.Float(), nullable=True),
        sa.Column("spot_price", sa.Float(), nullable=True),
        sa.Column("mark_price", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("venue", "symbol", "mts"),
    )
    op.create_index(
        "idx_perp_funding_symbol_mts",
        "perp_funding_rates",
        ["symbol", "mts"],
    )

    op.create_table(
        "liquidations",
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("pos_id", sa.BigInteger(), nullable=False),
        sa.Column("mts", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("base_price", sa.Float(), nullable=True),
        sa.Column("is_match", sa.Integer(), nullable=False),
        sa.Column("is_market_sold", sa.Integer(), nullable=False),
        sa.Column("price_acquired", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("venue", "pos_id", "mts", "is_match", "is_market_sold"),
    )
    op.create_index(
        "idx_liquidations_symbol_mts",
        "liquidations",
        ["symbol", "mts"],
    )


def downgrade() -> None:
    op.drop_index("idx_liquidations_symbol_mts", table_name="liquidations")
    op.drop_table("liquidations")
    op.drop_index("idx_perp_funding_symbol_mts", table_name="perp_funding_rates")
    op.drop_table("perp_funding_rates")
