"""Walk-back / top-up ingest service tests (sqlite + AsyncMock client)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.external_signals.repository import (
    upsert_liquidations,
    upsert_perp_funding,
)
from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)
from bfx_funding_bot.modules.external_signals.service import (
    backfill_liquidations_to_earliest,
    backfill_perp_funding_to_earliest,
    topup_binance_funding_to_latest,
    topup_liquidations_to_latest,
    topup_perp_funding_to_latest,
)
from bfx_funding_bot.modules.external_signals.tables import (
    LiquidationRow,
    PerpFundingRateRow,
)

_SYM = "tBTCF0:USTF0"
_NOW = 1_784_471_455_000


def _perp(mts: int, *, venue: str = "bitfinex", symbol: str = _SYM) -> PerpFundingRecord:
    return PerpFundingRecord(venue=venue, symbol=symbol, mts=mts, funding_rate=0.0001)


def _liq(pos_id: int, mts: int) -> LiquidationRecord:
    return LiquidationRecord(
        venue="bitfinex", pos_id=pos_id, mts=mts, symbol=_SYM,
        amount=-0.1, base_price=100.0, is_match=1, is_market_sold=1,
        price_acquired=101.0,
    )


@pytest_asyncio.fixture
async def _schema(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _perp_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(PerpFundingRateRow))
    ).scalar_one()


async def _liq_count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(LiquidationRow))
    ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestPerpWalkBack:
    async def test_walks_back_until_empty_page(self, sqlite_session: AsyncSession) -> None:
        client: Any = AsyncMock()
        page1 = [_perp(5000), _perp(4000), _perp(3000)]
        page2 = [_perp(2000), _perp(1000)]
        client.get_deriv_status_hist.side_effect = [page1, page2, []]

        stats = await backfill_perp_funding_to_earliest(
            client=client, session=sqlite_session, symbol=_SYM,
            page_limit=3, now_ms=_NOW,
        )
        await sqlite_session.commit()

        assert stats.pages == 2
        assert stats.rows == 5
        assert stats.done is True
        assert stats.earliest_mts == 1000
        assert await _perp_count(sqlite_session) == 5

        calls = client.get_deriv_status_hist.await_args_list
        assert calls[0].kwargs["end"] == _NOW
        assert calls[1].kwargs["end"] == 2999  # oldest(page1)-1
        # page2 is partial (2 < 3) → loop breaks; third call never happens
        assert len(calls) == 2

    async def test_resumes_from_db_min(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(sqlite_session, [_perp(9000), _perp(8000)])
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_deriv_status_hist.side_effect = [[]]

        stats = await backfill_perp_funding_to_earliest(
            client=client, session=sqlite_session, symbol=_SYM,
            page_limit=3, now_ms=_NOW,
        )
        assert client.get_deriv_status_hist.await_args_list[0].kwargs["end"] == 7999
        assert stats.done is True
        assert stats.earliest_mts == 8000

    async def test_max_pages_budget(self, sqlite_session: AsyncSession) -> None:
        client: Any = AsyncMock()
        client.get_deriv_status_hist.side_effect = [
            [_perp(5000), _perp(4000), _perp(3000)],
            [_perp(2900), _perp(2800), _perp(2700)],
        ]
        stats = await backfill_perp_funding_to_earliest(
            client=client, session=sqlite_session, symbol=_SYM,
            page_limit=3, max_pages=2, now_ms=_NOW,
        )
        assert stats.pages == 2
        assert stats.done is False  # budget exhausted, not naturally finished

    async def test_cursor_stuck_raises(self, sqlite_session: AsyncSession) -> None:
        client: Any = AsyncMock()
        client.get_deriv_status_hist.side_effect = [
            [_perp(_NOW + 10), _perp(_NOW + 5), _perp(_NOW + 1)],
        ]
        with pytest.raises(BackfillCursorStuck):
            await backfill_perp_funding_to_earliest(
                client=client, session=sqlite_session, symbol=_SYM,
                page_limit=3, now_ms=_NOW,
            )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestPerpTopUp:
    async def test_stops_at_db_max(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(sqlite_session, [_perp(3000)])
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_deriv_status_hist.side_effect = [
            [_perp(9000), _perp(8000), _perp(7000)],
            [_perp(6000), _perp(5000), _perp(3000)],  # reaches db_max
        ]
        stats = await topup_perp_funding_to_latest(
            client=client, session=sqlite_session, symbol=_SYM,
            page_limit=3, now_ms=_NOW,
        )
        await sqlite_session.commit()
        assert stats.done is True
        assert stats.pages == 2
        assert await _perp_count(sqlite_session) == 6  # 3000 upserted once
        assert stats.latest_mts == 9000

    async def test_empty_db_falls_back_to_full_walkback_semantics(
        self, sqlite_session: AsyncSession
    ) -> None:
        client: Any = AsyncMock()
        client.get_deriv_status_hist.side_effect = [[_perp(9000)]]
        stats = await topup_perp_funding_to_latest(
            client=client, session=sqlite_session, symbol=_SYM,
            page_limit=3, now_ms=_NOW,
        )
        assert stats.done is True
        assert stats.rows == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestLiquidationsWalkBack:
    async def test_overlap_cursor_refetches_boundary(self, sqlite_session: AsyncSession) -> None:
        client: Any = AsyncMock()
        page1 = [_liq(3, 5000), _liq(2, 4000), _liq(1, 3000)]
        page2 = [_liq(1, 3000), _liq(0, 1000)]  # boundary event re-served
        client.get_liquidations_hist.side_effect = [page1, page2]

        stats = await backfill_liquidations_to_earliest(
            client=client, session=sqlite_session, page_limit=3, now_ms=_NOW,
        )
        await sqlite_session.commit()

        calls = client.get_liquidations_hist.await_args_list
        assert calls[0].kwargs["end"] == _NOW
        assert calls[1].kwargs["end"] == 3000  # inclusive overlap, not -1
        assert stats.done is True
        assert await _liq_count(sqlite_session) == 4  # dedup via upsert

    async def test_no_progress_full_page_escapes_by_decrement(
        self, sqlite_session: AsyncSession
    ) -> None:
        client: Any = AsyncMock()
        same = [_liq(3, 3000), _liq(2, 3000), _liq(1, 3000)]
        client.get_liquidations_hist.side_effect = [same, same, []]
        stats = await backfill_liquidations_to_earliest(
            client=client, session=sqlite_session, page_limit=3, now_ms=_NOW,
        )
        calls = client.get_liquidations_hist.await_args_list
        assert calls[1].kwargs["end"] == 3000
        assert calls[2].kwargs["end"] == 2999  # escape decrement after no progress
        assert stats.done is True

    async def test_resume_and_empty_first_page(self, sqlite_session: AsyncSession) -> None:
        await upsert_liquidations(sqlite_session, [_liq(5, 7000)])
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_liquidations_hist.side_effect = [[]]
        stats = await backfill_liquidations_to_earliest(
            client=client, session=sqlite_session, page_limit=3, now_ms=_NOW,
        )
        # Inclusive resume cursor: events sharing db_min's millisecond may have
        # been cut off at the page boundary; re-fetch db_min itself (upsert dedups).
        assert client.get_liquidations_hist.await_args_list[0].kwargs["end"] == 7000
        assert stats.pages == 0
        assert stats.done is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestLiquidationsTopUp:
    async def test_stops_when_reaching_known_data(self, sqlite_session: AsyncSession) -> None:
        await upsert_liquidations(sqlite_session, [_liq(1, 3000)])
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_liquidations_hist.side_effect = [
            [_liq(4, 9000), _liq(3, 8000), _liq(2, 2500)],  # crosses db_max=3000
        ]
        stats = await topup_liquidations_to_latest(
            client=client, session=sqlite_session, page_limit=3, now_ms=_NOW,
        )
        assert stats.done is True
        assert stats.pages == 1
        assert await _liq_count(sqlite_session) == 4


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestBinanceForwardFill:
    async def test_walks_forward_until_partial_page(self, sqlite_session: AsyncSession) -> None:
        client: Any = AsyncMock()
        page1 = [
            _perp(1000, venue="binance-usdm", symbol="BTCUSDT"),
            _perp(2000, venue="binance-usdm", symbol="BTCUSDT"),
        ]
        page2 = [_perp(3000, venue="binance-usdm", symbol="BTCUSDT")]
        client.get_binance_funding.side_effect = [page1, page2]

        stats = await topup_binance_funding_to_latest(
            client=client, session=sqlite_session, symbol="BTCUSDT", page_limit=2,
        )
        await sqlite_session.commit()

        calls = client.get_binance_funding.await_args_list
        # startTime=0 is treated as absent by Binance (returns latest page
        # instead of history) — empty DB must start from 1, not 0.
        assert calls[0].kwargs["start_time"] == 1
        assert calls[1].kwargs["start_time"] == 2001
        assert stats.done is True
        assert stats.rows == 3
        assert stats.latest_mts == 3000

    async def test_resumes_from_db_max(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(
            sqlite_session, [_perp(5000, venue="binance-usdm", symbol="BTCUSDT")]
        )
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_binance_funding.side_effect = [[]]
        stats = await topup_binance_funding_to_latest(
            client=client, session=sqlite_session, symbol="BTCUSDT", page_limit=2,
        )
        assert client.get_binance_funding.await_args_list[0].kwargs["start_time"] == 5001
        assert stats.done is True

    async def test_explicit_start_time_overrides_resume(
        self, sqlite_session: AsyncSession
    ) -> None:
        """Full-history sweep despite existing recent rows (gap repair)."""
        await upsert_perp_funding(
            sqlite_session, [_perp(5000, venue="binance-usdm", symbol="BTCUSDT")]
        )
        await sqlite_session.flush()
        client: Any = AsyncMock()
        client.get_binance_funding.side_effect = [
            [_perp(100, venue="binance-usdm", symbol="BTCUSDT")]
        ]
        stats = await topup_binance_funding_to_latest(
            client=client, session=sqlite_session, symbol="BTCUSDT",
            page_limit=2, start_time=1,
        )
        assert client.get_binance_funding.await_args_list[0].kwargs["start_time"] == 1
        assert stats.done is True
