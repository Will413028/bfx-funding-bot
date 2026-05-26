"""G13: learn fill-rate stats from historical funding candles → fill_rate_stats.

Offline batch job (NOT wired into the live daemon). Run:
    cd backend_py && uv run python -m scripts.learn_fill_rate
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.lending.tracking.fill_rate import (
    BUCKET_GRID_BPS,
    HORIZONS_H,
    FillRateLearner,
)
from bfx_funding_bot.modules.lending.tracking.tables import FillRateStatsRow

# (symbol, timeframe, period_agg) series to learn — mirrors configs/cells.yaml.
SERIES_MATRIX: list[tuple[str, str, str]] = [
    ("fUSD", "1h", "p2"),
    ("fUSD", "1h", "p30"),
    ("fUSD", "1h", "a30"),
    ("fUST", "1h", "p2"),
    ("fUST", "1h", "p30"),
    ("fUST", "1h", "a30"),
]

_SOURCE = "candle"
_FAR_PAST_MS = 0
_FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01


async def learn_and_store(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    bucket_grid: list[int] | None = None,
    horizons: list[int] | None = None,
) -> int:
    """Learn stats for one series and persist (delete-then-insert, dialect-agnostic).

    Returns the number of stat rows written.
    """
    candles = await get_candles_in_range(
        session,
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        start_mts=_FAR_PAST_MS,
        end_mts=_FAR_FUTURE_MS,
    )
    if not candles:
        return 0
    learner = FillRateLearner(bucket_grid=bucket_grid, horizons=horizons)
    stats = learner.learn(candles)
    if not stats:
        return 0
    range_start = min(c.mts for c in candles)
    range_end = max(c.mts for c in candles)
    learned_at = datetime.now(UTC)

    await session.execute(
        delete(FillRateStatsRow).where(
            FillRateStatsRow.source == _SOURCE,
            FillRateStatsRow.symbol == symbol,
            FillRateStatsRow.period_agg == period_agg,
        )
    )
    session.add_all([
        FillRateStatsRow(
            source=_SOURCE,
            symbol=symbol,
            period_agg=period_agg,
            horizon_h=s.horizon_h,
            spread_bucket_bps=s.spread_bucket_bps,
            fill_prob=float(s.fill_prob),
            n_samples=s.n_samples,
            ttf_p50_ms=s.ttf_p50_ms,
            ttf_p90_ms=s.ttf_p90_ms,
            mean_ttf_ms=s.mean_ttf_ms,
            learned_at=learned_at,
            candle_range_start_ms=range_start,
            candle_range_end_ms=range_end,
        )
        for s in stats
    ])
    return len(stats)


async def main() -> None:
    settings = Settings()
    engine = make_async_engine_from_url(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    total = 0
    async with factory() as session:
        for symbol, timeframe, period_agg in SERIES_MATRIX:
            n = await learn_and_store(
                session,
                symbol=symbol,
                timeframe=timeframe,
                period_agg=period_agg,
                bucket_grid=BUCKET_GRID_BPS,
                horizons=HORIZONS_H,
            )
            print(f"{symbol} {timeframe} {period_agg}: wrote {n} stat rows")
            total += n
        await session.commit()
    await engine.dispose()
    print(f"done; {total} rows total")


if __name__ == "__main__":
    asyncio.run(main())
