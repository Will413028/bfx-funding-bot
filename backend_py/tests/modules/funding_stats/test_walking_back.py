from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_earliest,
)


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _stat(mts: int) -> FundingStat:
    return FundingStat(
        symbol="fUSD", mts=mts,
        frr=Decimal("5.8e-7"),
        avg_period=Decimal("2.3"),
        funding_amount=Decimal("4.5e7"),
        funding_amount_used=Decimal("2.1e7"),
        funding_below_threshold=Decimal("1.2e6"),
    )


@pytest.mark.asyncio
async def test_funding_stats_walks_back_until_empty(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    page1 = [_stat(1700007200000), _stat(1700003600000), _stat(1700000000000)]
    page2 = [_stat(1699996400000), _stat(1699992800000), _stat(1699989200000)]
    pages = [page1, page2, []]

    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = pages

    stats = await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.spec.kind == "funding_stats"
    assert stats.spec.symbol == "fUSD"
    assert stats.pages == 2
    assert stats.rows == 6
    assert stats.earliest_mts == 1699989200000


@pytest.mark.asyncio
async def test_funding_stats_resumes_from_db_min_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats

    await upsert_funding_stats(sqlite_session, [_stat(1700003600000)])
    await sqlite_session.commit()

    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.return_value = []

    await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=10,
    )

    call_kwargs = mock_client.get_funding_stats.await_args_list[0].kwargs
    assert call_kwargs["end"] == 1700003600000 - 1


@pytest.mark.asyncio
async def test_funding_stats_terminates_on_partial_page(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    page1 = [_stat(1700007200000), _stat(1700003600000), _stat(1700000000000)]
    page2 = [_stat(1699996400000)]
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.side_effect = [page1, page2]

    stats = await backfill_funding_stats_to_earliest(
        client=mock_client, session=sqlite_session,
        symbol="fUSD", page_limit=3,
    )
    await sqlite_session.commit()

    assert stats.pages == 2
    assert stats.rows == 4
    assert mock_client.get_funding_stats.await_count == 2


@pytest.mark.asyncio
async def test_funding_stats_raises_when_cursor_stuck(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats

    await upsert_funding_stats(sqlite_session, [_stat(1700000000001)])
    await sqlite_session.commit()

    bad_page = [_stat(1700000000000)]   # mts == end_ms after resume
    mock_client: Any = AsyncMock()
    mock_client.get_funding_stats.return_value = bad_page

    with pytest.raises(BackfillCursorStuck):
        await backfill_funding_stats_to_earliest(
            client=mock_client, session=sqlite_session,
            symbol="fUSD", page_limit=10,
        )
