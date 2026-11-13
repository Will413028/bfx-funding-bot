"""a9d2c4e6f813: one funding_candle_revisions row per refused value.

On a disposable PostgreSQL: repeats recorded before the key (a NULL value among them) collapse
to each value's earliest observation, and afterwards two writers recording the same value
leave one row.
"""
import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.candles.repository import mark_candles_final, upsert_candles
from tests.pg_templates import alembic, template_at

pytestmark = pytest.mark.integration

_BEFORE = "c3e7a91d5b20"
_MTS = 1704067200000


def _rev(conn, observed_at_ms: int, rejected_close: float | None) -> None:
    conn.execute(text(
        "INSERT INTO funding_candle_revisions (symbol, timeframe, period_agg, mts, observed_at_ms,"
        " rejected_close, final_close) VALUES ('fUST', '1h', 'p2', :m, :o, :r, 0.0002)"),
        {"m": _MTS, "o": observed_at_ms, "r": rejected_close})


@pytest.fixture
def before_url(pg_templates, pg_clone) -> str:
    """A database stopped at the previous revision (a later migration is irreversible, so it
    is built upward, not downgraded from head)."""
    return pg_clone(pg_templates.template("candle_revisions_before", template_at(_BEFORE)))


def test_upgrade_keeps_each_values_first_observation(before_url: str) -> None:
    engine = create_engine(before_url)
    try:
        with engine.begin() as conn:
            for observed_at_ms, value in ((40, 0.0001), (30, 0.0001), (50, 0.0003),
                                          (60, None), (70, None)):
                _rev(conn, observed_at_ms, value)
        alembic(before_url, "upgrade", "head")
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT observed_at_ms, rejected_close FROM funding_candle_revisions"
                " ORDER BY observed_at_ms")).all()
    finally:
        engine.dispose()
    assert [tuple(r) for r in rows] == [(30, 0.0001), (50, 0.0003), (60, None)]


@pytest.mark.asyncio
async def test_two_writers_seeing_the_same_refused_value_leave_one_row(pg_head_url: str) -> None:
    engine = create_async_engine(pg_head_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def candle(close: str) -> FundingCandle:
        return FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=_MTS,
                             open=close, close=close, high=close, low=close, volume="1")

    try:
        async with factory() as s:
            await upsert_candles(s, [candle("0.0002")], now_ms=1)
            await mark_candles_final(s, symbol="fUST", timeframe="1h", period_agg="p2",
                                     through_mts=_MTS, now_ms=2)
            await s.commit()
        # Two writers, each in its own open transaction, record the same refused value.
        # The second blocks on the first's uncommitted row until it commits.
        async with factory() as first, factory() as second:
            await upsert_candles(first, [candle("0.0001")], now_ms=3)

            async def second_writer() -> None:
                await upsert_candles(second, [candle("0.0001")], now_ms=4)
                await second.commit()

            pending = asyncio.create_task(second_writer())
            await asyncio.sleep(0.2)
            await first.commit()
            await asyncio.wait_for(pending, timeout=10)
        async with factory() as s:
            count = (await s.execute(text("SELECT count(*) FROM funding_candle_revisions"))).scalar()
    finally:
        await engine.dispose()
    assert count == 1


@pytest.mark.asyncio
async def test_a_refused_null_close_is_recorded_once(pg_head_url: str) -> None:
    """NULLS NOT DISTINCT is Postgres-only; the SQLite unit dialect cannot show this."""
    engine = create_async_engine(pg_head_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sealed = FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=_MTS,
                           open="0.0002", close="0.0002", high="0.0002", low="0.0002", volume="1")
    null_close = sealed.model_copy(update={"close": None})
    try:
        async with factory() as s:
            await upsert_candles(s, [sealed], now_ms=1)
            await mark_candles_final(s, symbol="fUST", timeframe="1h", period_agg="p2",
                                     through_mts=_MTS, now_ms=2)
            await s.commit()
        for now_ms in (3, 4):
            async with factory() as s:
                await upsert_candles(s, [null_close], now_ms=now_ms)
                await s.commit()
        async with factory() as s:
            rows = (await s.execute(text(
                "SELECT observed_at_ms, rejected_close FROM funding_candle_revisions"))).all()
    finally:
        await engine.dispose()
    assert [tuple(r) for r in rows] == [(3, None)]
