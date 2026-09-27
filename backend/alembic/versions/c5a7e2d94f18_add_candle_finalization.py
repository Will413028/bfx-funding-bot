"""add candle finalization + revision evidence

Revision ID: c5a7e2d94f18
Revises: f3b8d2c7a1e4
Create Date: 2026-07-27

ADR 2026-07-27 candle immutability (bitemporal).

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
    # migration exists to collect.
    #
    # The `mts <` bound excludes the period still forming right now. Sealing it
    # would (a) freeze its close at whatever half-formed value we happen to hold
    # and (b) make every legitimate WS update to it land in
    # funding_candle_revisions — polluting the exact table the ADR's revocation
    # trigger reads. Observed on the 2026-07-27 production run before this bound
    # existed: 8 false-positive revisions in 4 minutes, up to +67%. The in-flight
    # period needs no backfill anyway — CandleWriter seals it normally as soon as
    # the next mts arrives.
    op.execute(
        "UPDATE funding_candles SET is_final = true "
        "WHERE mts < (EXTRACT(EPOCH FROM date_trunc('hour', now())) * 1000)::bigint"
    )

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
