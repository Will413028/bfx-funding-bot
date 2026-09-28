from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter


async def test_candle_writer_upserts_from_queue(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()
    writer = CandleWriter(queue=queue, session_factory=factory, probe=HealthProbe())

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
    writer = CandleWriter(queue=queue, session_factory=factory, probe=HealthProbe())

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
    # Signature must track the real upsert_candles (now keyword-only now_ms) —
    # a stale fake raises TypeError on every call, which this test would report
    # as "the writer kept failing" rather than "the fake is out of date".
    async def flaky_upsert(session, candles, *, now_ms=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated db failure")
        # delegate to the real function for subsequent calls
        from bfx_funding_bot.modules.candles.repository import upsert_candles as _real
        await _real(session, candles, now_ms=now_ms)

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


import asyncpg  # noqa: E402


class TestCandleWriterErrorClassification:
    def test_transient_tuple_includes_interface_error(self):
        from bfx_funding_bot.modules.marketfeed.candle_writer import _DB_TRANSIENT
        assert asyncpg.InterfaceError in _DB_TRANSIENT
        assert asyncpg.ConnectionDoesNotExistError in _DB_TRANSIENT

    def test_fatal_tuple_includes_auth_errors(self):
        from bfx_funding_bot.modules.marketfeed.candle_writer import _DB_FATAL
        assert asyncpg.InvalidPasswordError in _DB_FATAL
        assert asyncpg.InvalidCatalogNameError in _DB_FATAL

    def test_taxonomy_classes_imported_in_module(self):
        """Verify candle_writer uses shared core/errors taxonomy."""
        import bfx_funding_bot.modules.marketfeed.candle_writer as mod
        with open(mod.__file__) as f:
            src = f.read()
        assert "from bfx_funding_bot.core.errors import" in src
        assert "FatalError" in src


async def test_writer_seals_previous_candle_when_the_period_advances(
    sqlite_engine: AsyncEngine,
):
    """A later mts arriving is the only reliable proof the previous period closed.

    Replaces guessing a settle delay (5s, then 30s — both wrong): the strategy
    may only observe a candle the venue has demonstrably moved past.
    """
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    queue: asyncio.Queue[CandleMessage | None] = asyncio.Queue()

    first, second = 1747584000000, 1747587600000
    # Advance the clock between messages so `first` is written while its period is
    # still open, then sealed only once the venue moves on to `second`.
    clock_values = iter([first + 1_800_000, second + 1_800_000])
    writer = CandleWriter(
        queue=queue, session_factory=factory, probe=HealthProbe(),
        clock=lambda: next(clock_values),
    )

    for mts in (first, second):
        await queue.put(CandleMessage(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=mts, open=0.0001, close=0.0001,
            high=0.0001, low=0.0001, volume=100.0,
        ))
    await queue.put(None)

    await writer.run()

    async with factory() as verify_session:
        rows = {
            r.mts: r
            for r in (
                await verify_session.execute(select(FundingCandleRow))
            ).scalars().all()
        }
        assert rows[first].is_final is True
        assert rows[first].finalized_at_ms is not None
        # the period now forming must stay open — it is still being re-pushed
        assert rows[second].is_final is False
