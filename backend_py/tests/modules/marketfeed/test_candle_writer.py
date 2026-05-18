from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter


async def test_candle_writer_upserts_from_queue(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()
    writer = CandleWriter(queue=queue, session_factory=factory)

    await queue.put(CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000, open=0.0001, close=0.0001,
        high=0.0001, low=0.0001, volume=100.0,
    ))
    await queue.put(None)

    await writer.run()

    # Verify with a fresh session — writer's sessions are closed per upsert
    async with factory() as verify_session:
        rows = (await verify_session.execute(select(FundingCandleRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].mts == 1747584000000


async def test_candle_writer_logs_and_continues_on_error(sqlite_engine: AsyncEngine):
    """Failure-path: upsert raises → writer logs + processes next candle (doesn't halt)."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()
    writer = CandleWriter(queue=queue, session_factory=factory)

    bad = CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000, open=0.0001, close=0.0001,
        high=0.0001, low=0.0001, volume=100.0,
    )
    good = CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000 + 3600_000, open=0.0001, close=0.0001,
        high=0.0001, low=0.0001, volume=100.0,
    )
    await queue.put(bad)
    await queue.put(good)
    await queue.put(None)

    call_count = 0
    async def flaky_upsert(session, candles):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated db failure")
        # delegate to the real function for subsequent calls
        from bfx_funding_bot.modules.candles.repository import upsert_candles as _real
        await _real(session, candles)

    with patch(
        "bfx_funding_bot.modules.marketfeed.candle_writer.upsert_candles",
        side_effect=flaky_upsert,
    ):
        await writer.run()  # must not raise — writer continues past the bad candle

    # The good candle should have been upserted
    async with factory() as verify_session:
        rows = (await verify_session.execute(select(FundingCandleRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].mts == 1747584000000 + 3600_000
