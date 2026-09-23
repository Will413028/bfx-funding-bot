"""Learn book-replay fill stats from recorded funding books → fill_rate_stats (source="book").

Offline batch job (NOT wired into the live daemon). Sibling of learn_fill_rate.py:
same tables, same artifact contract, `source="book"`, model `book-replay-v1`.
One artifact per (symbol, period series, horizon); stats are replaced
delete-then-insert per (symbol, period_agg) so an old artifact never keeps stale
rows alive (tech page Case 11).

Run from backend/ (env: DATABASE_URL). Snapshots exist only in the VM database:
  uv run python -m scripts.learn_book_fill_rate [--symbols fUST,fUSD] [--periods 2,30]
      [--offer-amount 150] [--since-ms N]
VM one-shot container: same pattern as scripts.audit_book_period_coverage.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.lending.tracking.book_replay import (
    BOOK_MODEL_VERSION,
    BOOK_SOURCE,
    DEFAULT_OFFER_AMOUNT,
    BookReplayLearner,
)
from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.repository import ensure_fill_model_artifact
from bfx_funding_bot.modules.lending.tracking.tables import FillRateStatsRow
from bfx_funding_bot.modules.marketfeed.book_period_coverage import (
    BookAskSnapshot,
    snapshot_from_payload,
)
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow
from scripts.learn_fill_rate import build_fill_model_artifact

_FAR_PAST_MS = 0
_FAR_FUTURE_MS = 4_102_444_800_000  # 2100-01-01


async def load_snapshots(
    session: AsyncSession, *, symbol: str, since_ms: int | None
) -> list[BookAskSnapshot]:
    stmt = select(
        FundingBookSnapshotRow.symbol,
        FundingBookSnapshotRow.captured_at_ms,
        FundingBookSnapshotRow.payload,
    ).where(FundingBookSnapshotRow.symbol == symbol)
    if since_ms is not None:
        stmt = stmt.where(FundingBookSnapshotRow.captured_at_ms >= since_ms)
    rows = (await session.execute(stmt.order_by(FundingBookSnapshotRow.captured_at_ms))).all()
    return [
        snapshot_from_payload(symbol=sym, captured_at_ms=ts, payload=payload)
        for sym, ts, payload in rows
    ]


async def learn_book_and_store(
    session: AsyncSession,
    *,
    symbol: str,
    period_days: int,
    offer_amount: Decimal = DEFAULT_OFFER_AMOUNT,
    since_ms: int | None = None,
    timeframe: str = "1h",
) -> list[BucketStat]:
    """Learn one (symbol, period) book model and persist it. Returns the stats written."""
    learner = BookReplayLearner(period_days=period_days, offer_amount=offer_amount)
    snapshots = await load_snapshots(session, symbol=symbol, since_ms=since_ms)
    if not snapshots:
        return []
    candles = await get_candles_in_range(
        session, symbol=symbol, timeframe=timeframe, period_agg=learner.period_agg,
        start_mts=_FAR_PAST_MS, end_mts=_FAR_FUTURE_MS,
    )
    if not candles:
        return []
    stats = learner.learn(snapshots, candles)
    if not stats:
        return []
    range_start = min(s.captured_at_ms for s in snapshots)
    range_end = max(s.captured_at_ms for s in snapshots)
    learned_at = datetime.now(UTC)

    await session.execute(
        delete(FillRateStatsRow).where(
            FillRateStatsRow.source == BOOK_SOURCE,
            FillRateStatsRow.symbol == symbol,
            FillRateStatsRow.period_agg == learner.period_agg,
        )
    )
    grouped: dict[int, list[BucketStat]] = {}
    for stat in stats:
        grouped.setdefault(stat.horizon_h, []).append(stat)
    for horizon_h, horizon_stats in sorted(grouped.items()):
        artifact = build_fill_model_artifact(
            source=BOOK_SOURCE, symbol=symbol, period_agg=learner.period_agg,
            horizon_h=horizon_h, stats=horizon_stats,
            training_start_ms=range_start, training_end_ms=range_end, timeframe=timeframe,
            model_version=BOOK_MODEL_VERSION,
            metadata_extra={
                "period_days": period_days,
                "offer_amount": str(offer_amount),
                "ref_lag_hours": learner.ref_lag_hours,
                "n_snapshots": len(snapshots),
            },
        )
        await ensure_fill_model_artifact(session, artifact=artifact, created_at=learned_at)
        session.add_all([
            FillRateStatsRow(
                source=BOOK_SOURCE, symbol=symbol, period_agg=learner.period_agg,
                horizon_h=stat.horizon_h, spread_bucket_bps=stat.spread_bucket_bps,
                fill_prob=float(stat.fill_prob), n_samples=stat.n_samples,
                ttf_p50_ms=stat.ttf_p50_ms, ttf_p90_ms=stat.ttf_p90_ms,
                mean_ttf_ms=stat.mean_ttf_ms, artifact_hash=artifact.artifact_hash,
                learned_at=learned_at,
                candle_range_start_ms=range_start, candle_range_end_ms=range_end,
            )
            for stat in horizon_stats
        ])
    return stats


def _render(symbol: str, period_days: int, stats: list[BucketStat]) -> str:
    lines = [f"{symbol} p{period_days}: {len(stats)} bucket stats",
             "  horizon  bps   fill_prob  n   ttf_p50"]
    for s in stats:
        p50 = f"{s.ttf_p50_ms / 60_000:.0f}m" if s.ttf_p50_ms is not None else "-"
        lines.append(f"  {s.horizon_h:>5}h {s.spread_bucket_bps:>5} {float(s.fill_prob):>9.3f} "
                     f"{s.n_samples:>4} {p50:>8}")
    return "\n".join(lines)


async def _amain(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--symbols", default="fUST,fUSD")
    p.add_argument("--periods", default="2,30")
    p.add_argument("--offer-amount", default=str(DEFAULT_OFFER_AMOUNT))
    p.add_argument("--since-ms", type=int, default=None)
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    periods = [int(x) for x in args.periods.split(",") if x.strip()]

    engine = make_engine(Settings())
    session_factory = make_session_factory(engine)
    try:
        for symbol in symbols:
            for period_days in periods:
                async with session_scope(session_factory) as session:
                    stats = await learn_book_and_store(
                        session, symbol=symbol, period_days=period_days,
                        offer_amount=Decimal(args.offer_amount), since_ms=args.since_ms,
                    )
                    await session.commit()
                print(_render(symbol, period_days, stats) if stats
                      else f"{symbol} p{period_days}: no snapshots/candles, nothing written")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
