from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking import tables as _t  # noqa: F401
from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.repository import load_fill_rate_stats
from scripts.learn_fill_rate import build_fill_model_artifact, learn_and_store

_H = 3_600_000


def test_fill_model_artifact_hash_is_stable_across_stat_order() -> None:
    stats = [
        BucketStat(4, 100, Decimal("0.5"), 80, 5_000, 9_000, 6_000),
        BucketStat(4, 0, Decimal("1"), 100, 1_000, 2_000, 1_500),
    ]

    first = build_fill_model_artifact(
        source="candle", symbol="fUSD", period_agg="p2", horizon_h=4,
        stats=stats, training_start_ms=0, training_end_ms=10_000, timeframe="1h",
    )
    second = build_fill_model_artifact(
        source="candle", symbol="fUSD", period_agg="p2", horizon_h=4,
        stats=list(reversed(stats)), training_start_ms=0, training_end_ms=10_000, timeframe="1h",
    )

    assert first.artifact_hash == second.artifact_hash
    assert first.sample_count == 180


@pytest.fixture
async def session_factory():
    engine = make_async_engine_from_url("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_learn_and_store_writes_stats(session_factory):
    candles = [
        FundingCandle(symbol="fUSD", timeframe="1h", period_agg="p2", mts=i * _H,
                      open=Decimal("0.0003"), close=Decimal("0.0003"),
                      high=Decimal("0.0003"), low=Decimal("0.0003"), volume=Decimal("0"))
        for i in range(30)
    ]
    async with session_factory() as s:
        await upsert_candles(s, candles)
        await s.commit()

    async with session_factory() as s:
        n = await learn_and_store(
            s, symbol="fUSD", timeframe="1h", period_agg="p2",
            bucket_grid=[-50, 0, 100], horizons=[1],
        )
        await s.commit()
    assert n == 3  # one row per bucket for the single horizon

    async with session_factory() as s:
        rows = await load_fill_rate_stats(s, source="candle", symbol="fUSD")
    assert len(rows) == 3
    assert all(r.candle_range_start_ms == 0 for r in rows)


async def test_learn_and_store_no_candles_writes_nothing(session_factory):
    async with session_factory() as s:
        n = await learn_and_store(s, symbol="fUSD", timeframe="1h", period_agg="p2")
        await s.commit()
    assert n == 0
