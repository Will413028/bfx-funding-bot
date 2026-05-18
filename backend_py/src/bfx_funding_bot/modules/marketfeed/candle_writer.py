"""WS candle queue -> funding_candles DB upsert.

Design basis: phase4.1-paper-shadow-infra-design.md Section "Data Flow" Flow 2
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle

log = logging.getLogger(__name__)


class CandleWriter:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[CandleMessage | None],
        session_factory: Callable[[], AsyncSession] | Callable[[], Awaitable[AsyncSession]],
    ) -> None:
        self._queue = queue
        self._session_factory = session_factory

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            if msg is None:
                return
            try:
                await self._upsert(msg)
            except Exception:
                log.exception("candle_writer_upsert_failed mts=%d", msg.mts)
                # continue - error logged; health_monitor surfaces from daemon side

    async def _upsert(self, msg: CandleMessage) -> None:
        candle = FundingCandle(
            symbol=msg.symbol,
            timeframe=msg.timeframe,
            period_agg=msg.period_agg,
            mts=msg.mts,
            open=Decimal(str(msg.open)),
            close=Decimal(str(msg.close)),
            high=Decimal(str(msg.high)),
            low=Decimal(str(msg.low)),
            volume=Decimal(str(msg.volume)),
        )
        maybe = self._session_factory()
        session: AsyncSession
        if asyncio.iscoroutine(maybe):
            session = await maybe
        else:
            session = maybe  # type: ignore[assignment]
        await upsert_candles(session, [candle])
        await session.commit()
