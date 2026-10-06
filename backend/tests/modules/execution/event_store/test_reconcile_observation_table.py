from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Every table the shared metadata may reach by foreign key, whatever was imported first.
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.uncertainty_tables
import bfx_funding_bot.modules.ledger.tables  # noqa: F401  (registers FK targets)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import ReconcileObservationRow


@pytest.mark.asyncio
async def test_reconcile_observation_is_append_only_insertable():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:  # type: AsyncSession
        session.add(ReconcileObservationRow(
            account_id="default", deployment_environment="ci",
            reserved_usdt=Decimal("100"), realized_usdt=Decimal("450"),
            n_offers=1, n_credits=3, observed_at_ms=1_000, event_seq_fence=42,
        ))
        await session.commit()
        rows = (await session.execute(
            select(ReconcileObservationRow).where(
                ReconcileObservationRow.account_id == "default")
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].realized_usdt == Decimal("450")
    assert rows[0].event_seq_fence == 42
    await engine.dispose()
