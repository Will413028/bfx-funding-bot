"""REST 缺口補齊 helper — warmup 跟 reconnect 共用.

設計依據: phase4.1-paper-shadow-infra-design.md Q8 / Q10
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle

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
