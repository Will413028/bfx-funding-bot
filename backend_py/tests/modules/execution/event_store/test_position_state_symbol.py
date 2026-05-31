from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
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
