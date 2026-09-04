from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow
from bfx_funding_bot.modules.execution.events import (
    SnapshotCoverage,
    VenueSnapshotObserved,
)

_ACCOUNT = "550e8400-e29b-41d4-a716-446655440000"


async def _engine_sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_position_state_has_symbol_and_renamed_columns():
    engine, sm = await _engine_sm()
    async with sm() as session:
        session.add(PositionStateRow(
            account_id="a", deployment_environment="ci", symbol="fUST",
            reserved=Decimal("10"), realized=Decimal("450"),
            last_updated_ms=1, last_event_seq=5))
        await session.commit()
        row = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a",
            PositionStateRow.symbol == "fUST"))).scalar_one()
    assert row.symbol == "fUST"
    assert row.reserved == Decimal("10")
    assert row.realized == Decimal("450")
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_symbols_coexist_for_same_account_env():
    """Composite PK (account, env, symbol) lets two currency rows live side by side."""
    engine, sm = await _engine_sm()
    async with sm() as session:
        session.add(PositionStateRow(
            account_id="a", deployment_environment="ci", symbol="fUST",
            reserved=Decimal("10"), realized=Decimal("100"),
            last_updated_ms=1, last_event_seq=1))
        session.add(PositionStateRow(
            account_id="a", deployment_environment="ci", symbol="fUSD",
            reserved=Decimal("20"), realized=Decimal("200"),
            last_updated_ms=1, last_event_seq=1))
        await session.commit()
        rows = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a",
            PositionStateRow.deployment_environment == "ci"))).scalars().all()
    by_symbol = {r.symbol: r for r in rows}
    assert set(by_symbol) == {"fUST", "fUSD"}
    assert by_symbol["fUST"].realized == Decimal("100")
    assert by_symbol["fUSD"].realized == Decimal("200")
    await engine.dispose()


@pytest.mark.asyncio
async def test_project_position_state_isolates_symbols():
    """_project_position_state mutates only the event.symbol bucket; a different
    symbol's row is untouched. A full-account observation seeds both rows."""
    engine, sm = await _engine_sm()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        await store.append(session, VenueSnapshotObserved(
            account_id=_ACCOUNT,
            environment="ci",
            query_started_at_ms=0,
            query_finished_at_ms=1,
            offers=(),
            credits=(
                VenueCreditObservation(
                    credit_id="c-ust", symbol="fUST", amount=Decimal("100"),
                    rate=None, period_days=2, status="ACTIVE",
                ),
                VenueCreditObservation(
                    credit_id="c-usd", symbol="fUSD", amount=Decimal("200"),
                    rate=None, period_days=2, status="ACTIVE",
                ),
            ),
            wallet_available={"fUST": Decimal("0"), "fUSD": Decimal("0")},
            coverage=SnapshotCoverage(
                active_offers_complete=True,
                active_credits_complete=True,
                wallets_complete=True,
            ),
            occurred_at_ms=1,
        ))
        await session.commit()

        # a CLAIMED projection on fUST must not touch fUSD
        await store._project_position_state(
            session, "RESERVATION_CLAIMED", _ACCOUNT, Decimal("30"),
            event_seq=10, occurred_at_ms=2, symbol="fUST")
        await session.commit()

        rows = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.exchange_account_id.is_not(None)
                )
            )
        ).scalars().all()
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["fUST"].reserved == Decimal("30")   # +30 claimed
    assert by_symbol["fUST"].realized == Decimal("100")  # unchanged
    assert by_symbol["fUSD"].reserved == Decimal("0")    # isolated
    assert by_symbol["fUSD"].realized == Decimal("200")  # isolated
    await engine.dispose()


@pytest.mark.asyncio
async def test_project_position_state_upserts_in_place_per_symbol():
    """Two events on the same symbol accumulate into ONE row, not two."""
    engine, sm = await _engine_sm()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        await store._project_position_state(
            session, "RESERVATION_CLAIMED", "a", Decimal("40"),
            event_seq=1, occurred_at_ms=1, symbol="fUST")
        await store._project_position_state(
            session, "RESERVATION_CLAIMED", "a", Decimal("10"),
            event_seq=2, occurred_at_ms=2, symbol="fUST")
        await session.commit()
        n = (await session.execute(select(func.count()).select_from(
            PositionStateRow).where(PositionStateRow.symbol == "fUST"))).scalar_one()
        row = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.symbol == "fUST"))).scalar_one()
    assert n == 1
    assert row.reserved == Decimal("50")   # 40 + 10 accumulated in place
    await engine.dispose()
