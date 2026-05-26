"""Persistence for G13 fill_rate_stats."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.tables import FillRateStatsRow

_PK = ["source", "symbol", "period_agg", "horizon_h", "spread_bucket_bps"]


async def upsert_fill_rate_stats(
    session: AsyncSession,
    *,
    source: str,
    symbol: str,
    period_agg: str,
    stats: list[BucketStat],
    candle_range_start_ms: int,
    candle_range_end_ms: int,
    learned_at: datetime,
) -> None:
    """Upsert stats by composite PK. SQLITE-ONLY (uses the sqlite ON CONFLICT
    dialect) — exercised by the unit tests. Production persistence goes through
    scripts/learn_fill_rate.py's dialect-agnostic delete-then-insert, NOT this.
    Do not call against Postgres without switching to a pg/dialect-neutral upsert.
    """
    if not stats:
        return
    values = [
        {
            "source": source,
            "symbol": symbol,
            "period_agg": period_agg,
            "horizon_h": s.horizon_h,
            "spread_bucket_bps": s.spread_bucket_bps,
            "fill_prob": float(s.fill_prob),
            "n_samples": s.n_samples,
            "ttf_p50_ms": s.ttf_p50_ms,
            "ttf_p90_ms": s.ttf_p90_ms,
            "mean_ttf_ms": s.mean_ttf_ms,
            "learned_at": learned_at,
            "candle_range_start_ms": candle_range_start_ms,
            "candle_range_end_ms": candle_range_end_ms,
        }
        for s in stats
    ]
    stmt = sqlite_insert(FillRateStatsRow).values(values)
    update_cols = {
        c: getattr(stmt.excluded, c)
        for c in (
            "fill_prob", "n_samples", "ttf_p50_ms", "ttf_p90_ms", "mean_ttf_ms",
            "learned_at", "candle_range_start_ms", "candle_range_end_ms",
        )
    }
    stmt = stmt.on_conflict_do_update(index_elements=_PK, set_=update_cols)
    await session.execute(stmt)


async def load_fill_rate_stats(
    session: AsyncSession, *, source: str, symbol: str
) -> list[FillRateStatsRow]:
    stmt = select(FillRateStatsRow).where(
        FillRateStatsRow.source == source,
        FillRateStatsRow.symbol == symbol,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
