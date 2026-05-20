"""CP4: Same DB seed candles → two independent warmups → strategy state byte-equal."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import (
    FundingCandleRow,  # noqa: F401 — registers table with Base.metadata
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })


@pytest.mark.integration
async def test_warmup_byte_equal_state(sqlite_session: AsyncSession) -> None:
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal(f"0.000{i+1}"),
            close=Decimal(f"0.000{i+1}"),
            high=Decimal(f"0.000{i+1}"),
            low=Decimal(f"0.000{i+1}"),
            volume=Decimal("100"),
        )
        for i in range(5)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    bfx = AsyncMock()
    bfx.get_funding_candles = AsyncMock(return_value=[])

    reg1 = StrategyRegistry()
    reg2 = StrategyRegistry()
    cell = _cell()

    await warmup_cell(
        cell=cell, registry=reg1, bitfinex=bfx,
        session=sqlite_session, now_mts=1747584000000 + 5 * 3600_000,
    )
    await warmup_cell(
        cell=cell, registry=reg2, bitfinex=bfx,
        session=sqlite_session, now_mts=1747584000000 + 5 * 3600_000,
    )

    s1 = reg1.get(cell)
    s2 = reg2.get(cell)

    # Compare internal deque state: both strategies must hold the same window.
    assert list(s1._window) == list(s2._window), (
        f"Window mismatch: {list(s1._window)} != {list(s2._window)}"
    )

    # Determinism proxy: same input candle → same decision.
    test_candle = FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000 + 6 * 3600_000,
        open=Decimal("0.0006"), close=Decimal("0.0006"),
        high=Decimal("0.0006"), low=Decimal("0.0006"),
        volume=Decimal("100"),
    )
    d1 = s1.decide(test_candle)
    d2 = s2.decide(test_candle)
    if d1 is None:
        assert d2 is None
    else:
        assert d2 is not None
        assert d1.rate == d2.rate
        assert d1.period_days == d2.period_days
