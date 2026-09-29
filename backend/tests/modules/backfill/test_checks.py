from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_round_trip,
    check_row_counts,
)
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.market import (
    SeriesSpec,
)


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _spec_candles(p: str = "p2") -> SeriesSpec:
    return SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg=p)


def _spec_fs(symbol: str = "fUSD") -> SeriesSpec:
    return SeriesSpec(kind="funding_stats", symbol=symbol)


# ---------- check_row_counts ----------

@pytest.mark.asyncio
async def test_row_counts_passes_when_all_have_rows(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                      mts=1700000000000, open=None, close=None,
                      high=None, low=None, volume=None),
    ])
    await upsert_funding_stats(sqlite_session, [
        FundingStat(symbol="fUSD", mts=1700000000000),
    ])
    await sqlite_session.commit()

    result = await check_row_counts(
        sqlite_session, [_spec_candles("p2"), _spec_fs("fUSD")]
    )
    assert result.passed is True
    assert result.failures == []


@pytest.mark.asyncio
async def test_row_counts_fails_for_empty_series(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    await upsert_candles(sqlite_session, [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                      mts=1700000000000, open=None, close=None,
                      high=None, low=None, volume=None),
    ])
    await sqlite_session.commit()

    # specs include one empty series (p30) and one populated (p2)
    result = await check_row_counts(
        sqlite_session, [_spec_candles("p2"), _spec_candles("p30")]
    )
    assert result.passed is False
    assert len(result.failures) == 1
    assert "p30" in result.failures[0]


# ---------- check_round_trip ----------

@pytest.mark.asyncio
async def test_round_trip_passes_when_db_matches_fresh_fetch(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    # DB needs >= 7 days of history so the shifted sample endpoint
    # (max_mts - 7d) is still >= min_mts. We insert an OLD candle (the
    # one we'll sample) and a newer marker candle 8 days later.
    eight_days_ms = 8 * 24 * 3_600_000
    old_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2",
        mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    newer_marker = old_candle.model_copy(
        update={"mts": 1700000000000 + eight_days_ms},
    )
    await upsert_candles(sqlite_session, [old_candle, newer_marker])
    await sqlite_session.commit()

    # Bitfinex returns the OLD (immutable) candle when asked end=max-7d.
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = [old_candle]
    mock_client.get_funding_stats.return_value = []

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_round_trip_fails_on_drift(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    eight_days_ms = 8 * 24 * 3_600_000
    stored_old = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    newer_marker = stored_old.model_copy(
        update={"mts": 1700000000000 + eight_days_ms},
    )
    await upsert_candles(sqlite_session, [stored_old, newer_marker])
    await sqlite_session.commit()

    drifted_old = stored_old.model_copy(update={"close": Decimal("0.0009")})
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = [drifted_old]
    mock_client.get_funding_stats.return_value = []

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is False


@pytest.mark.asyncio
async def test_round_trip_skipped_when_history_too_recent(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """DB has < 7 days of history → sample endpoint precedes min_mts → skip
    (treated as PASS) without calling Bitfinex."""
    candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2",
        mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    await upsert_candles(sqlite_session, [candle])
    await sqlite_session.commit()

    mock_client: Any = AsyncMock()
    # If get_funding_candles is called, return drifted data — test would fail.
    mock_client.get_funding_candles.return_value = [
        candle.model_copy(update={"close": Decimal("9.9999")}),
    ]
    mock_client.get_funding_stats.return_value = []

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is True
    assert "skipped" in result.message.lower()
    mock_client.get_funding_candles.assert_not_called()


# ---------- check_continuity ----------

@pytest.mark.asyncio
async def test_continuity_passes_for_dense_candles(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    # 10 hourly candles → 100% continuity
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1700000000000 + i * 3_600_000,
            open=None, close=None, high=None, low=None, volume=None,
        )
        for i in range(10)
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await check_continuity(sqlite_session, [_spec_candles("p2")])
    assert result.passed is True


@pytest.mark.asyncio
async def test_continuity_fails_when_gap_too_big(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    # 3 dense candles, then a 65-day jump (> 60-day candle threshold).
    sixty_five_days_h = 65 * 24
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=mts, open=None, close=None, high=None, low=None, volume=None,
        )
        for mts in [
            1700000000000,
            1700003600000,
            1700007200000,
            1700000000000 + sixty_five_days_h * 3_600_000,
        ]
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await check_continuity(sqlite_session, [_spec_candles("p2")])
    assert result.passed is False


@pytest.mark.asyncio
async def test_continuity_passes_for_sparse_p30_with_small_gaps(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """p30 (30-day funding) candles are inherently sparse — only 50% density
    over the time window, but max gap is small. New max-gap rule accepts
    this (would fail under old 95% count-ratio rule)."""
    base = 1700000000000
    # Pattern: every 2 hours for 100 hours → 50 rows, density 50%, max gap = 2h.
    mts_values = [base + h * 3_600_000 for h in range(0, 100, 2)]
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p30",
            mts=mts, open=None, close=None, high=None, low=None, volume=None,
        )
        for mts in mts_values
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    # Sanity: 50 rows over ~100h window → < 95% (would fail old rule).
    result = await check_continuity(sqlite_session, [_spec_candles("p30")])
    assert result.passed is True
