from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import backfill_candles


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_backfill_fetches_and_stores_then_returns_both(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    fake_candles = [
        FundingCandle(
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
    ]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = fake_candles

    fetched, stored = await backfill_candles(
        bitfinex=mock_client,
        session=sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_ms=1704067200000,
        end_ms=1704067200000,
        limit=125,
    )

    mock_client.get_funding_candles.assert_awaited_once_with(
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start=1704067200000,
        end=1704067200000,
        limit=125,
    )
    assert fetched == fake_candles
    assert len(stored) == 1
    assert stored[0].mts == 1704067200000
    assert stored[0].close is not None
    assert abs(stored[0].close - Decimal("0.0002")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_backfill_returns_empty_lists_when_bitfinex_returns_nothing(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = []

    fetched, stored = await backfill_candles(
        bitfinex=mock_client,
        session=sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_ms=0,
        end_ms=0,
        limit=10,
    )

    assert fetched == []
    assert stored == []
