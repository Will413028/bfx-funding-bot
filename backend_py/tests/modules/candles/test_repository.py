from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import (
    get_candles_in_range,
    upsert_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips_decimal(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    candles = [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704067200000,
            open=Decimal("0.0001234567"),
            close=Decimal("0.0001234568"),
            high=Decimal("0.0001234600"),
            low=Decimal("0.0001234500"),
            volume=Decimal("12345.678"),
        ),
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704070800000,
            open=Decimal("0.0001234568"),
            close=Decimal("0.0001234570"),
            high=Decimal("0.0001234700"),
            low=Decimal("0.0001234560"),
            volume=Decimal("11000.0"),
        ),
    ]

    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=1704067200000,
        end_mts=1704070800000,
    )

    assert len(fetched) == 2
    assert fetched[0].mts == 1704067200000
    assert fetched[1].mts == 1704070800000
    assert fetched[0].open is not None
    assert abs(fetched[0].open - Decimal("0.0001234567")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_replaces_existing_pk(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    original = FundingCandle(
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        mts=1704067200000,
        open=Decimal("0.0001"),
        close=Decimal("0.0002"),
        high=Decimal("0.0003"),
        low=Decimal("0.00005"),
        volume=Decimal("100.0"),
    )
    updated = original.model_copy(update={"close": Decimal("0.0009")})

    await upsert_candles(sqlite_session, [original])
    await upsert_candles(sqlite_session, [updated])
    await sqlite_session.commit()

    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=1704067200000,
        end_mts=1704067200000,
    )
    assert len(fetched) == 1
    assert fetched[0].close is not None
    assert abs(fetched[0].close - Decimal("0.0009")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_empty_list_is_noop(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [])
    await sqlite_session.commit()
    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=0,
        end_mts=10**13,
    )
    assert fetched == []
