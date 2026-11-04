"""REST 缺口補齊 helper — warmup、reconnect 與 tick 的 boundary 補齊共用.

設計依據: phase 4.1 paper/shadow infra design Q8 / Q10
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.tables import FundingCandleRow

log = logging.getLogger(__name__)

_TIMEFRAME_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}


class _BitfinexProtocol(Protocol):
    async def get_funding_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        period_agg: str,
        start: int,
        end: int,
        limit: int,
    ) -> list[FundingCandle]: ...


@dataclass(frozen=True)
class GapFillResult:
    """Result of a gap-fill REST + upsert operation.

    candles_fetched: count of candles returned by Bitfinex REST.
    candles_upserted: count of candles passed to upsert_candles() — equals
        candles_fetched, since upsert_candles uses ON CONFLICT DO UPDATE,
        treating every input row as "written" (insert or update; no skip).
    """
    candles_fetched: int
    candles_upserted: int


async def fill_gap_from_rest(
    *,
    bitfinex: _BitfinexProtocol,
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    period_agg: str,
    last_known_mts: int,
    now_mts: int,
    limit: int = 125,
) -> GapFillResult:
    """補齊 [last_known_mts+1, now_mts] 區間缺漏 candle 到 funding_candles 表."""
    step = _TIMEFRAME_MS.get(timeframe)
    if step is None:
        raise ValueError(f"unsupported timeframe {timeframe!r}")

    if now_mts - last_known_mts < step:
        return GapFillResult(0, 0)

    fetched = await bitfinex.get_funding_candles(
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        start=last_known_mts + 1,
        end=now_mts,
        limit=limit,
    )
    if not fetched:
        return GapFillResult(0, 0)
    await upsert_candles(session, fetched)
    await session.commit()
    upserted = len(fetched)
    log.info(
        "gap_fill symbol=%s timeframe=%s period_agg=%s fetched=%d upserted=%d",
        symbol,
        timeframe,
        period_agg,
        len(fetched),
        upserted,
    )
    return GapFillResult(candles_fetched=len(fetched), candles_upserted=upserted)


async def fill_missing_boundary(
    *,
    bitfinex: _BitfinexProtocol,
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    period_agg: str,
    boundary_mts: int,
    max_steps: int,
) -> bool:
    """Fill the candle that just closed (``boundary_mts``) from REST when nothing recorded it.

    The account's tick runs this before it seals and reads, so it does not depend on market
    data's recording (ADR 2026-10-19 D1, R9c). The window ends at the boundary and reaches
    back at most ``max_steps`` candles, or to the newest stored one if that is nearer: after
    a long outage the newest candles come back, not the oldest page of the gap. The upsert
    is idempotent (sealed rows are no-ops, a repeated refused value is one revision row).
    Returns whether REST was asked. REST errors propagate; the caller decides.
    """
    step = _TIMEFRAME_MS.get(timeframe)
    if step is None:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    series = (
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
    )
    present = (await session.execute(
        select(FundingCandleRow.mts).where(*series, FundingCandleRow.mts == boundary_mts)
    )).scalar_one_or_none()
    if present is not None:
        return False
    newest = (await session.execute(
        select(func.max(FundingCandleRow.mts)).where(*series, FundingCandleRow.mts < boundary_mts)
    )).scalar_one_or_none()
    floor = boundary_mts - max_steps * step
    start = max(int(newest) + 1, floor) if newest is not None else floor
    result = await fill_gap_from_rest(
        bitfinex=bitfinex, session=session, symbol=symbol, timeframe=timeframe,
        period_agg=period_agg, last_known_mts=start - 1, now_mts=boundary_mts,
        limit=max_steps + 1,
    )
    log.info(
        "boundary_gap_fill symbol=%s timeframe=%s period_agg=%s boundary_mts=%d fetched=%d",
        symbol, timeframe, period_agg, boundary_mts, result.candles_fetched,
    )
    return True
