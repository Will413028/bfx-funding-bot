from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.gap_fill import fill_gap_from_rest
from bfx_funding_bot.modules.candles.schemas import FundingCandle


async def test_fill_gap_returns_zero_when_no_gap(sqlite_session: AsyncSession):
    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])

    result = await fill_gap_from_rest(
        bitfinex=bfx, session=sqlite_session,
        symbol="fUSD", timeframe="1h", period_agg="a30",
        last_known_mts=1747584000000, now_mts=1747584000000 + 60_000,
    )

    assert result.candles_fetched == 0
    bfx.get_funding_candles.assert_not_called()


async def test_fill_gap_fetches_and_upserts(sqlite_session: AsyncSession):
    from bfx_funding_bot.core.db import Base
    from bfx_funding_bot.modules.candles.tables import FundingCandleRow  # noqa: F401
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(1, 4)
    ]
    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=fake_candles)

    result = await fill_gap_from_rest(
        bitfinex=bfx, session=sqlite_session,
        symbol="fUSD", timeframe="1h", period_agg="a30",
        last_known_mts=1747584000000,
        now_mts=1747584000000 + 4 * 3600_000,
    )

    assert result.candles_fetched == 3
