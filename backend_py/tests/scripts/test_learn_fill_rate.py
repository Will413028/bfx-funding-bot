from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking import tables as _t  # noqa: F401
from bfx_funding_bot.modules.lending.tracking.repository import load_fill_rate_stats
from scripts.learn_fill_rate import learn_and_store

_H = 3_600_000


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
