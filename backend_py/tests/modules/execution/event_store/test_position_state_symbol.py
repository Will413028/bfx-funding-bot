from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow


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
    symbol's row is untouched. set_position_snapshot seeds each symbol's row."""
    engine, sm = await _engine_sm()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        # seed two symbols via the snapshot writer
        await store.set_position_snapshot(
            session, account_id="a", reserved_usdt=Decimal("0"),
            realized_usdt=Decimal("100"), n_offers=0, n_credits=1,
            occurred_at_ms=1, symbol="fUST")
        await store.set_position_snapshot(
            session, account_id="a", reserved_usdt=Decimal("0"),
            realized_usdt=Decimal("200"), n_offers=0, n_credits=1,
            occurred_at_ms=1, symbol="fUSD")
        await session.commit()

        # a CLAIMED projection on fUST must not touch fUSD
        await store._project_position_state(
            session, "RESERVATION_CLAIMED", "a", Decimal("30"),
            event_seq=10, occurred_at_ms=2, symbol="fUST")
        await session.commit()

        rows = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalars().all()
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
