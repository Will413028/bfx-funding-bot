from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter


async def test_candle_writer_upserts_from_queue(sqlite_session: AsyncSession):
    async with sqlite_session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()
    writer = CandleWriter(queue=queue, session_factory=lambda: sqlite_session)

    await queue.put(CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000, open=0.0001, close=0.0001,
        high=0.0001, low=0.0001, volume=100.0,
    ))
    await queue.put(None)  # sentinel for stop

    await writer.run()

    rows = (await sqlite_session.execute(select(FundingCandleRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].mts == 1747584000000
