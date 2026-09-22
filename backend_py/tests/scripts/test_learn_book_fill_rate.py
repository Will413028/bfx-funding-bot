"""learn_book_and_store persists book-replay stats under source="book" with a versioned artifact."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.candles.tables
import bfx_funding_bot.modules.lending.tracking.tables
import bfx_funding_bot.modules.marketfeed.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.lending.tracking.book_replay import BOOK_MODEL_VERSION
from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.tables import (
    FillRateModelArtifactRow,
    FillRateStatsRow,
)
from bfx_funding_bot.modules.marketfeed.tables import FundingBookSnapshotRow
from scripts.learn_book_fill_rate import learn_book_and_store
from scripts.learn_fill_rate import _MODEL_VERSION, build_fill_model_artifact

_H = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


async def _seed(sf: async_sessionmaker, *, n_snapshots: int = 40) -> None:
    async with sf() as s:
        for i in range(n_snapshots + 6):
            s.add(FundingCandleRow(
                symbol="fUST", timeframe="1h", period_agg="p2", mts=_T0 + i * _H,
                open=0.0002, close=0.0002, high=0.0002, low=0.0002, volume=1000.0,
                is_final=True,
            ))
        for i in range(n_snapshots):
            t = _T0 + (i + 1) * _H + 600_000  # 10 minutes past each hour
            s.add(FundingBookSnapshotRow(
                symbol="fUST", captured_at_ms=t, best_ask_rate=Decimal("0.0002"),
                best_bid_rate=None, bid_depth=Decimal("0"), ask_depth=Decimal("2500"),
                payload={"asks": [[0.0002, 2, 5, 2500.0]], "bids": []},
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_learn_book_and_store_writes_book_rows_and_artifact(sf) -> None:
    await _seed(sf)
    async with sf() as s:
        stats = await learn_book_and_store(s, symbol="fUST", period_days=2)
        await s.commit()
    assert stats, "40 snapshots with a completed reference hour must yield stats"

    async with sf() as s:
        rows = (await s.execute(select(FillRateStatsRow))).scalars().all()
        artifacts = (await s.execute(select(FillRateModelArtifactRow))).scalars().all()
    assert rows and all(r.source == "book" and r.period_agg == "p2" for r in rows)
    assert {a.model_version for a in artifacts} == {BOOK_MODEL_VERSION}
    assert {a.source for a in artifacts} == {"book"}
    assert {r.artifact_hash for r in rows} == {a.artifact_hash for a in artifacts}
    assert all(a.metadata_json["period_days"] == 2 for a in artifacts)
    # Snapshots are 10 minutes past the hour: no candle fits inside a 1h horizon, so
    # no 1h rows; at par the offer needs 2650 of volume at 1000/h -> filled within 4h.
    by_key = {(r.horizon_h, r.spread_bucket_bps): r for r in rows}
    assert not any(h == 1 for h, _ in by_key)
    assert by_key[(4, 0)].fill_prob == 1.0


@pytest.mark.asyncio
async def test_rerun_replaces_rows_instead_of_accumulating(sf) -> None:
    await _seed(sf)
    async with sf() as s:
        first = await learn_book_and_store(s, symbol="fUST", period_days=2)
        await s.commit()
        second = await learn_book_and_store(s, symbol="fUST", period_days=2)
        await s.commit()
        rows = (await s.execute(select(FillRateStatsRow))).scalars().all()
    assert len(first) == len(second) == len(rows)


def test_candle_artifact_hash_is_unchanged_by_the_new_parameters() -> None:
    stats = [BucketStat(horizon_h=4, spread_bucket_bps=0, fill_prob=Decimal("0.5"),
                        n_samples=40, ttf_p50_ms=None, ttf_p90_ms=None, mean_ttf_ms=None)]
    kwargs: dict[str, object] = {
        "source": "candle", "symbol": "fUST", "period_agg": "p2", "horizon_h": 4,
        "stats": stats, "training_start_ms": 0, "training_end_ms": 1, "timeframe": "1h",
    }
    default = build_fill_model_artifact(**kwargs)  # type: ignore[arg-type]
    explicit = build_fill_model_artifact(model_version=_MODEL_VERSION, metadata_extra=None, **kwargs)  # type: ignore[arg-type]
    book = build_fill_model_artifact(model_version="book-replay-v1", metadata_extra={"period_days": 2}, **kwargs)  # type: ignore[arg-type]
    assert default.artifact_hash == explicit.artifact_hash
    assert book.artifact_hash != default.artifact_hash and book.model_version == "book-replay-v1"
    assert book.metadata["period_days"] == 2
    _ = datetime.now(UTC)
