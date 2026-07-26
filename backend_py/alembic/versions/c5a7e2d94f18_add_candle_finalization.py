"""add candle finalization + revision evidence

Revision ID: c5a7e2d94f18
Revises: f3b8d2c7a1e4
Create Date: 2026-07-27

ADR: wiki/projects/bfx-funding-bot/decisions/2026-07-27-candle-immutability-bitemporal.md

A candle is mutable while its period is still forming — Bitfinex keeps re-pushing
the same mts with a moving close, and the old in-place upsert let those late
revisions overwrite values the strategy had already observed. `is_final` marks the
rows that are safe to feed a strategy; `funding_candle_revisions` records the
writes we now refuse, so we can finally tell whether the venue revises sealed
candles at all (the ADR's revocation trigger).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a7e2d94f18"
down_revision: str | Sequence[str] | None = "f3b8d2c7a1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "funding_candles",
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "funding_candles",
        sa.Column("first_seen_at_ms", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "funding_candles",
        sa.Column("finalized_at_ms", sa.BigInteger(), nullable=True),
    )

    # CRITICAL — without this every historical candle stays is_final = false and
    # the strategy-facing reads (which default to final-only) return an empty
    # history, i.e. the bot goes blind the moment this deploys. Rows already in
    # the table are settled history and are treated as sealed.
    #
    # `finalized_at_ms` stays NULL for them: we genuinely do not know when they
    # closed, and inventing a timestamp would poison the very evidence this
    # migration exists to collect. The single newest row per series may still
    # have been forming at migration time — it gets sealed one period early,
    # which is bounded, one-off, and strictly safer than leaving it writable.
    op.execute("UPDATE funding_candles SET is_final = true")

    op.create_table(
        "funding_candle_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("period_agg", sa.Text(), nullable=False),
        sa.Column("mts", sa.BigInteger(), nullable=False),
        sa.Column("observed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("rejected_close", sa.Float(), nullable=True),
        sa.Column("final_close", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_funding_candle_revisions_series",
        "funding_candle_revisions",
        ["symbol", "timeframe", "period_agg", "mts"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_funding_candle_revisions_series",
        table_name="funding_candle_revisions",
    )
    op.drop_table("funding_candle_revisions")
    op.drop_column("funding_candles", "finalized_at_ms")
    op.drop_column("funding_candles", "first_seen_at_ms")
    op.drop_column("funding_candles", "is_final")
