from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_frr_unit,
    check_round_trip,
    check_row_counts,
)
from bfx_funding_bot.modules.backfill.schemas import (
    SeriesSpec,
)
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


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
    mock_client.get_funding_candles.return_value = [candle]
    mock_client.get_funding_stats.return_value = []

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_round_trip_fails_on_drift(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    stored = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1700000000000,
        open=Decimal("0.0001"), close=Decimal("0.0002"),
        high=Decimal("0.0003"), low=Decimal("0.00005"),
        volume=Decimal("100"),
    )
    await upsert_candles(sqlite_session, [stored])
    await sqlite_session.commit()

    drifted = stored.model_copy(update={"close": Decimal("0.0009")})
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = [drifted]

    result = await check_round_trip(
        sqlite_session, mock_client, [_spec_candles("p2")],
    )
    assert result.passed is False


# ---------- check_frr_unit ----------

@pytest.mark.asyncio
async def test_frr_unit_passes_when_ratio_in_range(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """FRR=5.8e-7/sec * 86400 ~= 5e-2/day; candle close ~ 1e-4/day-equivalent.
    Ratio = 5e-2 / 1e-4 = 500 -- fails the [0.1, 10] band.
    Use plausible values: FRR=5.8e-7, candle close=5e-2 -> ratio=1.0 (good)."""
    await upsert_funding_stats(sqlite_session, [FundingStat(
        symbol="fUSD", mts=1700000000000, frr=Decimal("5.8e-7"),
    )])
    await upsert_candles(sqlite_session, [FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p2", mts=1700001000000,
        open=None, close=Decimal("5e-2"),
        high=None, low=None, volume=None,
    )])
    await sqlite_session.commit()

    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is True


@pytest.mark.asyncio
async def test_frr_unit_fails_when_ratio_out_of_range(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """FRR=5.8e-7 * 86400 = 5e-2; candle close = 1e-4. Ratio = 500 -> fails."""
    await upsert_funding_stats(sqlite_session, [FundingStat(
        symbol="fUSD", mts=1700000000000, frr=Decimal("5.8e-7"),
    )])
    await upsert_candles(sqlite_session, [FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p2", mts=1700001000000,
        open=None, close=Decimal("1e-4"),
        high=None, low=None, volume=None,
    )])
    await sqlite_session.commit()

    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is False
    assert "ratio" in result.message.lower()


@pytest.mark.asyncio
async def test_frr_unit_skips_when_no_data(
    sqlite_session: AsyncSession, setup_schema: None,
) -> None:
    """No funding_stats or no candles → check is skipped (not a failure)."""
    result = await check_frr_unit(sqlite_session, symbol="fUSD")
    assert result.passed is True   # skip = pass
    assert "skip" in result.message.lower() or "no data" in result.message.lower()


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
    # 5 candles at hour 0,1,2 — then jumps to hour 100 (50% missing)
    candles = [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=mts, open=None, close=None, high=None, low=None, volume=None,
        )
        for mts in [
            1700000000000,
            1700003600000,
            1700007200000,
            1700000000000 + 100 * 3_600_000,
        ]
    ]
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await check_continuity(sqlite_session, [_spec_candles("p2")])
    assert result.passed is False
