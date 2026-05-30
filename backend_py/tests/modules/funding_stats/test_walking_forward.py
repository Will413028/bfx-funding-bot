"""Unit tests for backfill_funding_stats_to_latest (forward fill).

Uses the same fake-REST-client + SQLite in-memory pattern as test_walking_back.py.
`now_ms` is injected explicitly so tests are fully deterministic — no wall-clock.
"""
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_latest,
)


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _stat(mts: int, symbol: str = "fUSD") -> FundingStat:
    return FundingStat(
        symbol=symbol,
        mts=mts,
        frr=Decimal("5.8e-7"),
        avg_period=Decimal("2.3"),
        funding_amount=Decimal("4.5e7"),
        funding_amount_used=Decimal("2.1e7"),
        funding_below_threshold=Decimal("1.2e6"),
    )


# ---------------------------------------------------------------------------
# Test 1: empty DB (db_max=None) — stops on short page
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_empty_db_stops_on_short_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """With no prior rows, a single short page ends the loop (pages=1)."""
    now_ms = 1717000000000

    # Short page (2 items, page_limit=3) → terminates immediately
    short_page = [_stat(1716996400000), _stat(1716992800000)]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = [short_page]

    stats = await backfill_funding_stats_to_latest(
        client=mock_client,
        session=sqlite_session,
        symbol="fUSD",
        page_limit=3,
        now_ms=now_ms,
    )
    await sqlite_session.commit()

    assert stats.spec.kind == "funding_stats"
    assert stats.spec.symbol == "fUSD"
    assert stats.pages == 1
    assert stats.rows == 2
    # first call should use now_ms as end
    call_kwargs = mock_client.get_funding_stats.await_args_list[0].kwargs
    assert call_kwargs["end"] == now_ms


# ---------------------------------------------------------------------------
# Test 2: db_max set — stops when page's oldest_mts <= db_max (gap filled)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_stops_at_db_max(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """When oldest_mts of a page reaches db_max, loop stops (gap filled)."""
    db_max_mts = 1700000000000
    await upsert_funding_stats(sqlite_session, [_stat(db_max_mts)])
    await sqlite_session.commit()

    now_ms = 1717000000000
    # Page spans back past db_max — oldest entry is <= db_max
    page = [
        _stat(1716996400000),
        _stat(1716992800000),
        _stat(1699999000000),  # older than db_max_mts → gap is covered
    ]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = [page]

    stats = await backfill_funding_stats_to_latest(
        client=mock_client,
        session=sqlite_session,
        symbol="fUSD",
        page_limit=10,
        now_ms=now_ms,
    )
    await sqlite_session.commit()

    assert stats.pages == 1
    assert stats.rows == 3
    # Only one API call — stopped after seeing oldest_mts <= db_max
    assert mock_client.get_funding_stats.await_count == 1


# ---------------------------------------------------------------------------
# Test 3: multi-page gap — pages until oldest_mts crosses db_max
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_fill_multi_page_stops_at_db_max(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Multi-page walk: stops exactly when a page's oldest_mts <= db_max."""
    db_max_mts = 1700000000000
    await upsert_funding_stats(sqlite_session, [_stat(db_max_mts)])
    await sqlite_session.commit()

    now_ms = 1717000000000

    # Page 1: entirely above db_max → cursor moves back, full page (3 items)
    page1 = [
        _stat(1717000000000 - 3600000),   # newest
        _stat(1717000000000 - 7200000),
        _stat(1717000000000 - 10800000),  # oldest still above db_max
    ]
    # Page 2: oldest_mts <= db_max → stops
    page2 = [
        _stat(1717000000000 - 14400000),
        _stat(1700002000000),
        _stat(1699990000000),  # oldest < db_max → break
    ]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = [page1, page2]

    stats = await backfill_funding_stats_to_latest(
        client=mock_client,
        session=sqlite_session,
        symbol="fUSD",
        page_limit=3,
        now_ms=now_ms,
    )
    await sqlite_session.commit()

    # Should have fetched exactly 2 pages
    assert stats.pages == 2
    assert stats.rows == 6
    assert mock_client.get_funding_stats.await_count == 2

    # Verify cursor walked back: second call's end should be page1's oldest - 1
    call2_kwargs = mock_client.get_funding_stats.await_args_list[1].kwargs
    expected_end = min(s.mts for s in page1) - 1
    assert call2_kwargs["end"] == expected_end
