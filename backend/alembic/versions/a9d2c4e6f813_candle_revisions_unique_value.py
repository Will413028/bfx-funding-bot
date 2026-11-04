"""One ``funding_candle_revisions`` row per refused value.

Several writers can see the same refused value of a sealed candle again: the public WS's
reconnect gap-fill, the worker's boundary gap-fill (R9c), and reruns of either. Each used to
append a row, so the table counted observations, not revisions. A unique key on the value
makes a repeat a no-op (writers insert ``ON CONFLICT DO NOTHING``); only the first
observation of a value survives (ADR 2026-10-19 D4, brief D-B).

Existing repeats are deleted first, keeping each value's earliest observation (lowest
``observed_at_ms``, then lowest ``id``): the later rows carry no value the kept row lacks,
only a later sighting of it. Production held 48,120 rows, 47,400 of them repeats (counted
2026-11-04); Will approved deleting them (the pre-deploy backup keeps them). ``rejected_close`` can be NULL, so the key treats NULLs as
equal (``NULLS NOT DISTINCT``, Postgres 15+). The old series index is a prefix of the new
key and goes.

The table is locked for the revision; bfx-deploy stops the webapi and the bot first.
Downgrade restores the series index; deleted repeats do not come back.
"""
from __future__ import annotations

from alembic import op

revision = "a9d2c4e6f813"
down_revision = "c3e7a91d5b20"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

TABLE = "funding_candle_revisions"


def upgrade() -> None:
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"""
        DELETE FROM {TABLE} WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY symbol, timeframe, period_agg, mts, rejected_close
                    ORDER BY observed_at_ms, id
                ) AS sighting
                FROM {TABLE}
            ) ranked
            WHERE sighting > 1
        )
        """
    )
    # squawk's CONCURRENTLY rules guard against blocking writers on a live table; this one is
    # already locked above, bfx-deploy has stopped the bot and the webapi (its only writers),
    # it holds about 720 rows after the delete, and CONCURRENTLY cannot run inside the
    # revision's transaction.
    op.execute(
        "-- squawk-ignore require-concurrent-index-deletion\n"
        "DROP INDEX idx_funding_candle_revisions_series"
    )
    op.execute(
        "-- squawk-ignore require-concurrent-index-creation\n"
        f"CREATE UNIQUE INDEX uq_funding_candle_revisions_value ON {TABLE} "
        "(symbol, timeframe, period_agg, mts, rejected_close) NULLS NOT DISTINCT"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_funding_candle_revisions_value")
    op.execute(
        f"CREATE INDEX idx_funding_candle_revisions_series ON {TABLE} "
        "(symbol, timeframe, period_agg, mts)"
    )
