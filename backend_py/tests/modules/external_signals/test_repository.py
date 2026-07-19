"""Idempotent upsert + mts-bounds tests for external_signals repository (sqlite)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.external_signals.repository import (
    get_liq_max_mts,
    get_liq_min_mts,
    get_perp_max_mts,
    get_perp_min_mts,
    upsert_liquidations,
    upsert_perp_funding,
)
from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)
from bfx_funding_bot.modules.external_signals.tables import (
    LiquidationRow,
    PerpFundingRateRow,
)


def _perp(mts: int, *, rate: float = 0.0001, symbol: str = "tBTCF0:USTF0") -> PerpFundingRecord:
    return PerpFundingRecord(
        venue="bitfinex", symbol=symbol, mts=mts, funding_rate=rate,
        next_funding_accrued=0.0002, next_funding_evt_mts=mts + 3_600_000,
        deriv_price=64000.0, spot_price=64010.0, mark_price=64005.0,
        open_interest=8000.0,
    )


def _liq(pos_id: int, mts: int, *, amount: float = -0.1) -> LiquidationRecord:
    return LiquidationRecord(
        venue="bitfinex", pos_id=pos_id, mts=mts, symbol="tETHF0:USTF0",
        amount=amount, base_price=1845.5, is_match=1, is_market_sold=1,
        price_acquired=1877.2,
    )


@pytest_asyncio.fixture
async def _schema(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestPerpFundingRepository:
    async def test_upsert_is_idempotent_and_updates(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(sqlite_session, [_perp(1000), _perp(2000)])
        await upsert_perp_funding(sqlite_session, [_perp(2000, rate=0.0009)])
        await sqlite_session.commit()

        n = (
            await sqlite_session.execute(
                select(func.count()).select_from(PerpFundingRateRow)
            )
        ).scalar_one()
        assert n == 2
        updated = (
            await sqlite_session.execute(
                select(PerpFundingRateRow.funding_rate).where(PerpFundingRateRow.mts == 2000)
            )
        ).scalar_one()
        assert updated == 0.0009

    async def test_empty_list_is_noop(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(sqlite_session, [])

    async def test_bounds_filter_by_venue_and_symbol(self, sqlite_session: AsyncSession) -> None:
        await upsert_perp_funding(
            sqlite_session,
            [_perp(1000), _perp(3000), _perp(2000, symbol="tETHF0:USTF0")],
        )
        await sqlite_session.flush()

        assert await get_perp_min_mts(
            sqlite_session, venue="bitfinex", symbol="tBTCF0:USTF0"
        ) == 1000
        assert await get_perp_max_mts(
            sqlite_session, venue="bitfinex", symbol="tBTCF0:USTF0"
        ) == 3000
        assert await get_perp_min_mts(
            sqlite_session, venue="binance-usdm", symbol="BTCUSDT"
        ) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_schema")
class TestLiquidationRepository:
    async def test_upsert_is_idempotent_and_updates(self, sqlite_session: AsyncSession) -> None:
        await upsert_liquidations(sqlite_session, [_liq(1, 1000), _liq(2, 2000)])
        await upsert_liquidations(sqlite_session, [_liq(2, 2000, amount=-0.5)])
        await sqlite_session.commit()

        n = (
            await sqlite_session.execute(select(func.count()).select_from(LiquidationRow))
        ).scalar_one()
        assert n == 2
        updated = (
            await sqlite_session.execute(
                select(LiquidationRow.amount).where(LiquidationRow.pos_id == 2)
            )
        ).scalar_one()
        assert updated == -0.5

    async def test_same_pos_id_distinct_events_kept(self, sqlite_session: AsyncSession) -> None:
        initial = LiquidationRecord(
            venue="bitfinex", pos_id=7, mts=1000, symbol="tETHF0:USTF0",
            amount=-0.1, base_price=1845.5, is_match=0, is_market_sold=1,
            price_acquired=None,
        )
        matched = _liq(7, 1002)
        await upsert_liquidations(sqlite_session, [initial, matched])
        await sqlite_session.flush()
        n = (
            await sqlite_session.execute(select(func.count()).select_from(LiquidationRow))
        ).scalar_one()
        assert n == 2

    async def test_bounds(self, sqlite_session: AsyncSession) -> None:
        await upsert_liquidations(sqlite_session, [_liq(1, 500), _liq(2, 900)])
        await sqlite_session.flush()
        assert await get_liq_min_mts(sqlite_session, venue="bitfinex") == 500
        assert await get_liq_max_mts(sqlite_session, venue="bitfinex") == 900
        assert await get_liq_min_mts(sqlite_session, venue="other") is None
