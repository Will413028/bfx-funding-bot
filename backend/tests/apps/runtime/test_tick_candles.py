"""The tick's candle read does not depend on market data's recording (R9c).

With nothing recording candles, the tick asks REST for the boundary; a REST failure or a
venue that never answers leaves the gap to LOCF instead of failing or stalling the tick.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.apps.runtime.tick_candles import read_tick_candles
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.candles.repository import mark_candles_final, upsert_candles
from bfx_funding_bot.modules.strategy import CellConfig

HOUR = 3_600_000
BOUNDARY = 1_704_067_200_000


def _cell() -> CellConfig:
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUST", "period_agg": "p2", "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5}, "reference_amount_usdt": 150.0,
    })
    cell.staleness_budget_hours = 6
    return cell


def _candle(mts: int) -> FundingCandle:
    d = Decimal("0.0002")
    return FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
                         open=d, close=d, high=d, low=d, volume=Decimal("1"))


class _Rest:
    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.fail, self.hang, self.calls = fail, hang, 0

    async def get_funding_candles(self, **kw: Any) -> list[FundingCandle]:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise ConnectionError("venue unreachable")
        first = -(-kw["start"] // HOUR) * HOUR  # the first candle open at or after start
        return [_candle(m) for m in range(first, kw["end"] + 1, HOUR)]


@pytest.fixture
async def factory(sqlite_engine: AsyncEngine) -> Any:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with f() as s:  # recorded up to two hours before the boundary, then nothing
        await upsert_candles(s, [_candle(BOUNDARY - 3 * HOUR), _candle(BOUNDARY - 2 * HOUR)])
        await mark_candles_final(s, symbol="fUST", timeframe="1h", period_agg="p2",
                                 through_mts=BOUNDARY - 2 * HOUR)
        await s.commit()
    return f


async def _read(factory: Any, rest: _Rest, timeout_s: float = 10.0) -> list[int]:
    candles = await read_tick_candles(
        session_factory=factory, bitfinex=cast(Any, rest), cell=_cell(),
        candle_mts=BOUNDARY, lookback=7, timeout_s=timeout_s)
    return [c.mts for c in candles]


async def test_the_tick_reads_a_boundary_only_rest_recorded(factory: Any) -> None:
    rest = _Rest()
    assert await _read(factory, rest) == [BOUNDARY - 3 * HOUR, BOUNDARY - 2 * HOUR,
                                          BOUNDARY - HOUR, BOUNDARY]
    assert rest.calls == 1


async def test_an_unreachable_venue_leaves_the_gap_to_locf(factory: Any) -> None:
    assert await _read(factory, _Rest(fail=True)) == [BOUNDARY - 3 * HOUR, BOUNDARY - 2 * HOUR]


async def test_a_venue_that_never_answers_does_not_stall_the_tick(factory: Any) -> None:
    mts = await asyncio.wait_for(_read(factory, _Rest(hang=True), timeout_s=0.05), timeout=5)
    assert mts == [BOUNDARY - 3 * HOUR, BOUNDARY - 2 * HOUR]
