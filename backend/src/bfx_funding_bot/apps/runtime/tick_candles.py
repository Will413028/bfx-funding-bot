"""The candles a scheduler tick reads: fill the boundary if nothing recorded it, seal, read.

The account's tick must not depend on market data's recording (ADR 2026-10-19 D1, R9c), so
a missing boundary candle is asked of REST first (idempotent; bounded in time and window). A
REST failure or timeout leaves the gap to the caller's LOCF / hard-tier path.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.candles.gap_fill import fill_missing_boundary
from bfx_funding_bot.modules.candles.repository import get_up_to, seal_closed_periods
from bfx_funding_bot.modules.strategy import CellConfig

log = logging.getLogger(__name__)

# Bounded so a slow venue cannot stall the scheduler (its liveness threshold is 65 min; a
# tick normally takes well under a second).
BOUNDARY_GAP_FILL_TIMEOUT_S = 10.0


async def read_tick_candles(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    bitfinex: BitfinexREST,
    cell: CellConfig,
    candle_mts: int,
    lookback: int,
    timeout_s: float = BOUNDARY_GAP_FILL_TIMEOUT_S,
) -> list[FundingCandle]:
    try:
        async with session_factory() as s:
            await asyncio.wait_for(fill_missing_boundary(
                bitfinex=bitfinex, session=s, symbol=cell.symbol, timeframe=cell.timeframe,
                period_agg=cell.period_agg, boundary_mts=candle_mts, max_steps=lookback,
            ), timeout=timeout_s)
    except Exception:
        log.warning("boundary_gap_fill_failed cell=%s candle_mts=%d",
                    cell.pair_id, candle_mts, exc_info=True)
    async with session_factory() as s:
        # Seal on elapsed time before reading. CandleWriter only seals when a LATER mts
        # lands, so a slow venue push leaves mts=candle_mts unsealed at exactly the moment
        # we need it — a final-only read then skips it, LOCF fills the slot from the
        # previous candle, and the live observe sequence gains a hole that replay does not
        # have.
        await seal_closed_periods(
            s, symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
        )
        await s.commit()
        return list(await get_up_to(
            s, symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
            mts_inclusive=candle_mts, lookback=lookback,
        ))
