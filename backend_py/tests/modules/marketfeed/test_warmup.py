from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })


async def test_warmup_feeds_strategy_with_db_candles(sqlite_session: AsyncSession):
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal("0.0001"),
            close=Decimal(f"0.0001{i}").quantize(Decimal("0.00001")),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(5)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])
    reg = StrategyRegistry()
    cell = _cell()

    result = await warmup_cell(
        cell=cell, registry=reg,
        bitfinex=bfx, session=sqlite_session,
        now_mts=1747584000000 + 5 * 3600_000,
    )

    assert result.observed_count == 5
    assert reg.get(cell) is not None
