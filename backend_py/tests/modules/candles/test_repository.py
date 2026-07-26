from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import (
    get_candles_in_range,
    get_min_mts,
    get_up_to,
    mark_candles_final,
    upsert_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import (
    FundingCandleRevisionRow,
    FundingCandleRow,
)


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips_decimal(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    candles = [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704067200000,
            open=Decimal("0.0001234567"),
            close=Decimal("0.0001234568"),
            high=Decimal("0.0001234600"),
            low=Decimal("0.0001234500"),
            volume=Decimal("12345.678"),
        ),
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704070800000,
            open=Decimal("0.0001234568"),
            close=Decimal("0.0001234570"),
            high=Decimal("0.0001234700"),
            low=Decimal("0.0001234560"),
            volume=Decimal("11000.0"),
        ),
    ]

    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=1704067200000,
        end_mts=1704070800000,
        final_only=False,  # asserting what was written
    )

    assert len(fetched) == 2
    assert fetched[0].mts == 1704067200000
    assert fetched[1].mts == 1704070800000
    assert fetched[0].open is not None
    assert abs(fetched[0].open - Decimal("0.0001234567")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_replaces_existing_pk_while_the_period_is_still_forming(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """A forming candle must keep absorbing the venue's updates.

    This is the half of the rule that stays permissive: sealing only kicks in
    once the period closes (see test_upsert_does_not_overwrite_a_finalized_candle).
    """
    original = FundingCandle(
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        mts=1704067200000,
        open=Decimal("0.0001"),
        close=Decimal("0.0002"),
        high=Decimal("0.0003"),
        low=Decimal("0.00005"),
        volume=Decimal("100.0"),
    )
    updated = original.model_copy(update={"close": Decimal("0.0009")})
    # now inside the same hour as mts -> the period has not closed yet
    now = 1704067200000 + 1_800_000

    await upsert_candles(sqlite_session, [original], now_ms=now)
    await upsert_candles(sqlite_session, [updated], now_ms=now)
    await sqlite_session.commit()

    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=1704067200000,
        end_mts=1704067200000,
        final_only=False,  # asserting what was written
    )
    assert len(fetched) == 1
    assert fetched[0].close is not None
    assert abs(fetched[0].close - Decimal("0.0009")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_empty_list_is_noop(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [])
    await sqlite_session.commit()
    fetched = await get_candles_in_range(
        sqlite_session,
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        start_mts=0,
        end_mts=10**13,
        final_only=False,  # asserting what was written
    )
    assert fetched == []


@pytest.mark.asyncio
async def test_get_min_mts_returns_none_for_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    result = await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_min_mts_returns_smallest(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=mts, open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for mts in [1700003600000, 1700000000000, 1700007200000]
    ]
    # different (timeframe, period_agg) — must not contaminate min
    candles.append(FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p30",
        mts=1500000000000, open=None, close=None, high=None, low=None, volume=None,
    ))

    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    assert await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
    ) == 1700000000000
    assert await get_min_mts(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p30",
    ) == 1500000000000


@pytest.mark.asyncio
async def test_upsert_candles_chunks_large_input(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """5000 candles in one upsert call must succeed (forces > _CANDLE_CHUNK chunking)."""
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1700000000000 + i * 3600000,
            open=Decimal("0.0001"), close=Decimal("0.0002"),
            high=Decimal("0.0003"), low=Decimal("0.00005"),
            volume=Decimal("100"),
        )
        for i in range(5000)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()
    fetched = await get_candles_in_range(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        start_mts=1700000000000, end_mts=1700000000000 + 5000 * 3600000,
        final_only=False,  # asserting what was written, not what a strategy may read
    )
    assert len(fetched) == 5000


def _candle(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal("0.0001"), close=Decimal(close),
        high=Decimal("0.0003"), low=Decimal("0.00005"), volume=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_newly_upserted_candle_is_not_final(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """A candle that just arrived over WS is still forming — never final on write."""
    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.0002")], now_ms=111)
    await sqlite_session.commit()

    row = (
        await sqlite_session.execute(
            select(FundingCandleRow).where(FundingCandleRow.mts == 1704067200000)
        )
    ).scalar_one()
    assert row.is_final is False
    assert row.first_seen_at_ms == 111
    assert row.finalized_at_ms is None


@pytest.mark.asyncio
async def test_mark_candles_final_seals_rows_through_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Sealing at boundary T marks every earlier open candle final, not just one."""
    await upsert_candles(
        sqlite_session,
        [_candle(1704067200000, "0.0002"), _candle(1704070800000, "0.0003")],
        now_ms=111,
    )
    await sqlite_session.commit()

    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    rows = {
        r.mts: r
        for r in (await sqlite_session.execute(select(FundingCandleRow))).scalars()
    }
    assert rows[1704067200000].is_final is True
    assert rows[1704067200000].finalized_at_ms == 222
    # the still-forming candle at the boundary itself stays open
    assert rows[1704070800000].is_final is False
    assert rows[1704070800000].finalized_at_ms is None


@pytest.mark.asyncio
async def test_upsert_does_not_overwrite_a_finalized_candle(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """The whole point: once sealed, a candle's value can never move again.

    This is the bug that let live EMA drift — the venue re-pushed mts=T with a
    different close long after the strategy had already observed it.
    """
    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.0002")], now_ms=111)
    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    # venue re-pushes the same slot with a revised close
    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.00014999")], now_ms=333)
    await sqlite_session.commit()

    row = (
        await sqlite_session.execute(
            select(FundingCandleRow).where(FundingCandleRow.mts == 1704067200000)
        )
    ).scalar_one()
    assert row.close == pytest.approx(0.0002)
    assert row.finalized_at_ms == 222


@pytest.mark.asyncio
async def test_rejected_revision_of_a_final_candle_is_recorded(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Refusing the write is not enough — we must be able to prove it happened.

    Feeds the ADR revocation trigger: if the venue never actually revises sealed
    candles, this table stays empty and the bitemporal layer can be dropped.
    """
    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.0002")], now_ms=111)
    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.00014999")], now_ms=333)
    await sqlite_session.commit()

    revs = (
        await sqlite_session.execute(select(FundingCandleRevisionRow))
    ).scalars().all()
    assert len(revs) == 1
    assert revs[0].mts == 1704067200000
    assert revs[0].observed_at_ms == 333
    assert revs[0].rejected_close == pytest.approx(0.00014999)
    assert revs[0].final_close == pytest.approx(0.0002)


@pytest.mark.asyncio
async def test_identical_repush_of_a_final_candle_is_not_recorded(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Only a CHANGED value is evidence; re-sending the same close is noise."""
    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.0002")], now_ms=111)
    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    await upsert_candles(sqlite_session, [_candle(1704067200000, "0.0002")], now_ms=333)
    await sqlite_session.commit()

    revs = (
        await sqlite_session.execute(select(FundingCandleRevisionRow))
    ).scalars().all()
    assert revs == []


@pytest.mark.asyncio
async def test_get_up_to_hides_the_still_forming_candle(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Strategies must never see the in-flight period — that IS the bug.

    Default is final-only so a new call site is safe by construction; opting out
    has to be explicit.
    """
    await upsert_candles(
        sqlite_session,
        [_candle(1704067200000, "0.0002"), _candle(1704070800000, "0.0003")],
        now_ms=111,
    )
    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    visible = await get_up_to(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        mts_inclusive=1704070800000, lookback=10,
    )
    assert [c.mts for c in visible] == [1704067200000]

    including_open = await get_up_to(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        mts_inclusive=1704070800000, lookback=10, final_only=False,
    )
    assert [c.mts for c in including_open] == [1704067200000, 1704070800000]


@pytest.mark.asyncio
async def test_get_candles_in_range_hides_the_still_forming_candle(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """warmup reads through this function, so it must hide unsealed candles too.

    Missing this was the third root cause: get_up_to (divergence replay) filtered
    to final while get_candles_in_range (warmup) did not, so live state absorbed
    the in-flight candle and replay never did. The two arms diverged by exactly
    one observe() of a low, still-forming close.
    """
    await upsert_candles(
        sqlite_session,
        [_candle(1704067200000, "0.0002"), _candle(1704070800000, "0.0003")],
        now_ms=111,
    )
    await mark_candles_final(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        through_mts=1704067200000, now_ms=222,
    )
    await sqlite_session.commit()

    sealed_only = await get_candles_in_range(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        start_mts=0, end_mts=1704070800000,
    )
    assert [c.mts for c in sealed_only] == [1704067200000]

    everything = await get_candles_in_range(
        sqlite_session, symbol="fUST", timeframe="1h", period_agg="p2",
        start_mts=0, end_mts=1704070800000, final_only=False,
    )
    assert [c.mts for c in everything] == [1704067200000, 1704070800000]


@pytest.mark.asyncio
async def test_upsert_seals_candles_from_periods_already_past(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """REST backfill writes settled history — it must land sealed, not open.

    Otherwise final-only readers (warmup, replay, backtest) cannot see anything
    backfill produced, and fetch_and_store's read-back returns empty.
    """
    now = 1704074400000  # 2024-01-01 02:00 UTC — the 02:00 period is forming
    past = 1704067200000  # 00:00, closed
    forming = 1704074400000  # 02:00, current

    await upsert_candles(
        sqlite_session, [_candle(past, "0.0002"), _candle(forming, "0.0003")], now_ms=now
    )
    await sqlite_session.commit()

    rows = {
        r.mts: r
        for r in (await sqlite_session.execute(select(FundingCandleRow))).scalars()
    }
    assert rows[past].is_final is True, "a closed period must land sealed"
    assert rows[forming].is_final is False, "the forming period must stay open"
