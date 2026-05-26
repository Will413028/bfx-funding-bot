from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.lending.tracking import tables as _t  # noqa: F401
from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel
from bfx_funding_bot.modules.lending.tracking.repository import (
    load_fill_rate_stats,
    upsert_fill_rate_stats,
)


@pytest.fixture
async def session_factory():
    engine = make_async_engine_from_url("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _stats():
    return [
        BucketStat(horizon_h=4, spread_bucket_bps=0, fill_prob=Decimal("1.0"),
                   n_samples=100, ttf_p50_ms=1000, ttf_p90_ms=2000, mean_ttf_ms=1500),
        BucketStat(horizon_h=4, spread_bucket_bps=100, fill_prob=Decimal("0.5"),
                   n_samples=80, ttf_p50_ms=5000, ttf_p90_ms=9000, mean_ttf_ms=6000),
    ]


async def test_upsert_then_load_roundtrip(session_factory):
    async with session_factory() as s:
        await upsert_fill_rate_stats(
            s, source="candle", symbol="fUSD", period_agg="p2", stats=_stats(),
            candle_range_start_ms=1000, candle_range_end_ms=2000,
            learned_at=datetime.now(UTC),
        )
        await s.commit()
    async with session_factory() as s:
        rows = await load_fill_rate_stats(s, source="candle", symbol="fUSD")
    assert len(rows) == 2
    model = FillRateModel.from_rows(rows)
    est = model.estimate_fill(reference_rate=Decimal("0.0003"),
                              offer_rate=Decimal("0.0003"), period_agg="p2", horizon_h=4)
    assert est is not None and est.fill_prob == Decimal("1.0")


async def test_upsert_is_idempotent(session_factory):
    async with session_factory() as s:
        for _ in range(2):
            await upsert_fill_rate_stats(
                s, source="candle", symbol="fUSD", period_agg="p2", stats=_stats(),
                candle_range_start_ms=1000, candle_range_end_ms=2000,
                learned_at=datetime.now(UTC),
            )
            await s.commit()
        rows = await load_fill_rate_stats(s, source="candle", symbol="fUSD")
    assert len(rows) == 2  # upsert, not duplicate insert
