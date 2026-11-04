"""The worker's tick fills a missing boundary candle from REST (ADR 2026-10-19 D1, R9c).

With market data down (no candle WS recording) the account still needs the candle that just
closed; REST answers it, the upsert is idempotent and lands it sealed, so a rerun or the WS
writer arriving later changes nothing.
"""
from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.candles.gap_fill import fill_missing_boundary
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.tables import FundingCandleRevisionRow, FundingCandleRow

HOUR = 3_600_000
BOUNDARY = 1_704_067_200_000  # the candle that just closed (its open time)
NOW = BOUNDARY + HOUR + 5_000


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _candle(mts: int, close: str = "0.0002") -> FundingCandle:
    return FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
                         open=Decimal(close), close=Decimal(close), high=Decimal(close),
                         low=Decimal(close), volume=Decimal("1"))


class _Rest:
    def __init__(self, candles: list[FundingCandle]) -> None:
        self.candles = candles
        self.calls: list[dict[str, object]] = []

    async def get_funding_candles(self, **kw: object) -> list[FundingCandle]:
        self.calls.append(kw)
        start, end = cast(int, kw["start"]), cast(int, kw["end"])
        return [c for c in self.candles if start <= c.mts <= end]


async def _fill(session: AsyncSession, rest: _Rest, *, max_steps: int = 24) -> bool:
    return await fill_missing_boundary(
        bitfinex=rest, session=session, symbol="fUST", timeframe="1h", period_agg="p2",
        boundary_mts=BOUNDARY, max_steps=max_steps)


async def test_a_missing_boundary_is_filled_sealed_from_a_bounded_window(
        sqlite_session: AsyncSession, setup_schema: None) -> None:
    """Market data stopped three candles ago: REST is asked for the gap up to the boundary."""
    await upsert_candles(sqlite_session, [_candle(BOUNDARY - 3 * HOUR)], now_ms=NOW)
    await sqlite_session.commit()
    rest = _Rest([_candle(BOUNDARY - k * HOUR) for k in range(0, 4)])

    assert await _fill(sqlite_session, rest) is True

    assert [(c["start"], c["end"]) for c in rest.calls] == [(BOUNDARY - 3 * HOUR + 1, BOUNDARY)]
    row = (await sqlite_session.execute(select(FundingCandleRow).where(
        FundingCandleRow.mts == BOUNDARY))).scalar_one()
    assert row.is_final is True


async def test_a_long_outage_still_reaches_the_boundary(
        sqlite_session: AsyncSession, setup_schema: None) -> None:
    """Weeks without recording: the window is anchored on the boundary, not on the last
    stored candle, so the newest candles come back (not the oldest page of the gap)."""
    await upsert_candles(sqlite_session, [_candle(BOUNDARY - 500 * HOUR)], now_ms=NOW)
    await sqlite_session.commit()
    rest = _Rest([_candle(BOUNDARY - k * HOUR) for k in range(0, 600)])

    assert await _fill(sqlite_session, rest, max_steps=24) is True

    assert [(c["start"], c["end"]) for c in rest.calls] == [(BOUNDARY - 24 * HOUR, BOUNDARY)]


async def test_a_present_boundary_asks_nothing(
        sqlite_session: AsyncSession, setup_schema: None) -> None:
    await upsert_candles(sqlite_session, [_candle(BOUNDARY)], now_ms=NOW)
    await sqlite_session.commit()
    rest = _Rest([_candle(BOUNDARY)])

    assert await _fill(sqlite_session, rest) is False
    assert rest.calls == []


async def test_a_rerun_leaves_the_same_rows_and_no_revision(
        sqlite_session: AsyncSession, setup_schema: None) -> None:
    rest = _Rest([_candle(BOUNDARY - k * HOUR) for k in range(0, 4)])
    await _fill(sqlite_session, rest)
    before = (await sqlite_session.execute(select(FundingCandleRow.mts, FundingCandleRow.close,
                                                  FundingCandleRow.is_final))).all()
    # Forced rerun (e.g. a second worker): the same sealed rows, no refused-write evidence.
    await upsert_candles(sqlite_session, rest.candles, now_ms=NOW + 1)
    await sqlite_session.commit()

    after = (await sqlite_session.execute(select(FundingCandleRow.mts, FundingCandleRow.close,
                                                 FundingCandleRow.is_final))).all()
    assert sorted(after) == sorted(before)
    assert (await sqlite_session.execute(select(FundingCandleRevisionRow))).all() == []
