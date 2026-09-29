from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import backfill_candles_to_earliest
from bfx_funding_bot.modules.market import BackfillCursorStuck, SeriesSpec


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _candle(mts: int) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2",
        mts=mts,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_walks_back_until_empty_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Two pages of 3 candles each, then empty → loop terminates after page 3."""
    page1 = [_candle(1700007200000), _candle(1700003600000), _candle(1700000000000)]
    page2 = [_candle(1699996400000), _candle(1699992800000), _candle(1699989200000)]
    pages = [page1, page2, []]

    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.side_effect = pages

    spec = SeriesSpec(kind="candles", symbol="fUST", timeframe="1h", period_agg="p2")
    stats = await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.spec == spec
    assert stats.pages == 2     # page 3 was empty, didn't count
    assert stats.rows == 6
    assert stats.earliest_mts == 1699989200000
    assert mock_client.get_funding_candles.await_count == 3


@pytest.mark.asyncio
async def test_walks_back_terminates_on_partial_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """First page is full, second is half-full → second triggers early stop."""
    page1 = [_candle(1700007200000), _candle(1700003600000), _candle(1700000000000)]
    page2 = [_candle(1699996400000)]   # < limit
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.side_effect = [page1, page2]

    stats = await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.pages == 2
    assert stats.rows == 4
    assert mock_client.get_funding_candles.await_count == 2  # no 3rd call


@pytest.mark.asyncio
async def test_resumes_from_db_min_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """When DB already has data, cursor starts at db_min_mts - 1, not now."""
    from bfx_funding_bot.modules.candles.repository import upsert_candles

    # Seed DB with two existing rows
    seeded = [_candle(1700007200000), _candle(1700003600000)]
    await upsert_candles(sqlite_session, seeded)
    await sqlite_session.commit()

    # Mock returns empty (already at earliest from resume cursor's POV)
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = []

    await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=10,
    )

    # First call should have end = 1700003600000 - 1 (smallest existing - 1)
    call_kwargs = mock_client.get_funding_candles.await_args_list[0].kwargs
    assert call_kwargs["end"] == 1700003600000 - 1


@pytest.mark.asyncio
async def test_walks_back_starts_from_now_when_db_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Empty DB → first page's end is roughly now_ms (verify ≥ 1.7e12 i.e. > 2023)."""
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = []

    await backfill_candles_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUST", timeframe="1h", period_agg="p2",
        page_limit=10,
    )

    call_kwargs = mock_client.get_funding_candles.await_args_list[0].kwargs
    assert call_kwargs["end"] > 1_700_000_000_000   # > 2023-11-15 ms


@pytest.mark.asyncio
async def test_raises_when_cursor_stuck(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """If Bitfinex returns a row whose mts >= end_ms, raise BackfillCursorStuck."""
    # Force start from end_ms = 1700000000000 by seeding one row at 1700000000001
    from bfx_funding_bot.modules.candles.repository import upsert_candles

    await upsert_candles(sqlite_session, [_candle(1700000000001)])
    await sqlite_session.commit()
    # Now resume cursor = 1700000000001 - 1 = 1700000000000

    # Mock returns a row whose mts == 1700000000000 (NOT strictly less)
    bad_page = [_candle(1700000000000)]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_candles.return_value = bad_page

    with pytest.raises(BackfillCursorStuck):
        await backfill_candles_to_earliest(
            client=mock_client, session=sqlite_session,
            symbol="fUST", timeframe="1h", period_agg="p2",
            page_limit=10,
        )
