"""G13: learn fill-rate stats from historical funding candles → fill_rate_stats.

Offline batch job (NOT wired into the live daemon). Run:
    cd backend_py && uv run python -m scripts.learn_fill_rate
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.fill_rate import (
    BUCKET_GRID_BPS,
    HORIZONS_H,
    MIN_SAMPLES,
    BucketStat,
    FillRateLearner,
)
from bfx_funding_bot.modules.lending.tracking.repository import ensure_fill_model_artifact
from bfx_funding_bot.modules.lending.tracking.tables import (
    FillRateStatsRow,
)

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
_MODEL_VERSION = "g13-candle-v2"  # v2: horizon counts only candles wholly inside the window
_SCHEMA_VERSION = 1


def build_fill_model_artifact(
    *,
    source: str,
    symbol: str,
    period_agg: str,
    horizon_h: int,
    stats: list[BucketStat],
    training_start_ms: int,
    training_end_ms: int,
    timeframe: str,
    model_version: str = _MODEL_VERSION,
    metadata_extra: Mapping[str, object] | None = None,
) -> FillModelArtifact:
    """Build a stable artifact identity from canonical learned evidence.

    `model_version` / `metadata_extra` let sibling learners (book replay) share the
    same artifact contract; the defaults leave every candle artifact hash unchanged.
    """
    canonical_stats = [
        {
            "spread_bucket_bps": stat.spread_bucket_bps,
            "fill_prob": str(stat.fill_prob),
            "n_samples": stat.n_samples,
            "ttf_p50_ms": stat.ttf_p50_ms,
            "ttf_p90_ms": stat.ttf_p90_ms,
            "mean_ttf_ms": stat.mean_ttf_ms,
        }
        for stat in sorted(stats, key=lambda value: value.spread_bucket_bps)
    ]
    metadata: dict[str, object] = {
        "bucket_grid_bps": [stat["spread_bucket_bps"] for stat in canonical_stats],
        "timeframe": timeframe,
        **dict(metadata_extra or {}),
    }
    canonical = {
        "source": source,
        "symbol": symbol,
        "period_agg": period_agg,
        "horizon_h": horizon_h,
        "model_version": model_version,
        "schema_version": _SCHEMA_VERSION,
        "training_start_ms": training_start_ms,
        "training_end_ms": training_end_ms,
        "cutoff_ms": training_end_ms,
        "confidence_min_samples": MIN_SAMPLES,
        "metadata": metadata,
        "stats": canonical_stats,
    }
    artifact_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FillModelArtifact(
        symbol=symbol,
        period_agg=period_agg,
        horizon_h=horizon_h,
        source=source,
        model_version=model_version,
        schema_version=_SCHEMA_VERSION,
        artifact_hash=artifact_hash,
        training_start_ms=training_start_ms,
        training_end_ms=training_end_ms,
        cutoff_ms=training_end_ms,
        sample_count=sum(stat.n_samples for stat in stats),
        confidence_min_samples=MIN_SAMPLES,
        metadata=metadata,
    )


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
    grouped: dict[int, list[BucketStat]] = {}
    for stat in stats:
        grouped.setdefault(stat.horizon_h, []).append(stat)
    for horizon_h, horizon_stats in sorted(grouped.items()):
        artifact = build_fill_model_artifact(
            source=_SOURCE,
            symbol=symbol,
            period_agg=period_agg,
            horizon_h=horizon_h,
            stats=horizon_stats,
            training_start_ms=range_start,
            training_end_ms=range_end,
            timeframe=timeframe,
        )
        await ensure_fill_model_artifact(
            session,
            artifact=artifact,
            created_at=learned_at,
        )
        session.add_all([
            FillRateStatsRow(
                source=_SOURCE,
                symbol=symbol,
                period_agg=period_agg,
                horizon_h=stat.horizon_h,
                spread_bucket_bps=stat.spread_bucket_bps,
                fill_prob=float(stat.fill_prob),
                n_samples=stat.n_samples,
                ttf_p50_ms=stat.ttf_p50_ms,
                ttf_p90_ms=stat.ttf_p90_ms,
                mean_ttf_ms=stat.mean_ttf_ms,
                artifact_hash=artifact.artifact_hash,
                learned_at=learned_at,
                candle_range_start_ms=range_start,
                candle_range_end_ms=range_end,
            )
            for stat in horizon_stats
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
