"""R4.1.2: WS upsert happening 0-5s into the scheduler buffer window must
not produce a divergence between what scheduler reads at +5s and the
eventual "final" candle row.

Strategy: race candle_writer upserts with simulated late-arriving Bitfinex
candle revisions; verify scheduler always reads the LATEST upserted close.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.tables import FundingCandleRow  # noqa: F401
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe


@pytest.mark.integration
async def test_late_revision_within_buffer_window(sqlite_engine: AsyncEngine) -> None:
    """Last-write-wins: a candle revision arriving within the scheduler's
    5s buffer must overwrite the prior version in the DB."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    mts = 1747584000000
    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()

    initial = CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30", mts=mts,
        open=0.0001, close=0.00010, high=0.00010, low=0.00010, volume=100.0,
    )
    revision = CandleMessage(
        symbol="fUSD", timeframe="1h", period_agg="a30", mts=mts,
        open=0.0001, close=0.00012, high=0.00012, low=0.00010, volume=120.0,
    )
    await queue.put(initial)
    await queue.put(revision)
    await queue.put(None)

    writer = CandleWriter(queue=queue, session_factory=factory, probe=HealthProbe())
    await writer.run()

    async with factory() as verify_session:
        rows = await get_candles_in_range(
            verify_session, symbol="fUSD", timeframe="1h",
            period_agg="a30", start_mts=mts, end_mts=mts,
        )
    assert len(rows) == 1
    assert float(rows[0].close) == 0.00012  # latest revision wins
