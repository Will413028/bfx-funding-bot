from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    ReconcileObservationRow,
)


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_set_position_snapshot_writes_state_observation_and_returns_drift():
    engine, sm = await _session()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        # seed prior belief: realized=300 (the drifted-low ledger)
        session.add(PositionStateRow(
            account_id="a", deployment_environment="ci",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("300"),
            last_updated_ms=1, last_event_seq=5))
        # an event_log head at seq=5
        session.add(EventLogRow(
            account_id="a", deployment_environment="ci", event_type="ORDER_FILL",
            cid=1, venue_offer_id="v", venue_seq=1, payload={}, occurred_at_ms=1))
        await session.commit()

        drift = await store.set_position_snapshot(
            session, account_id="a",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, occurred_at_ms=2_000)
        await session.commit()

        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalar_one()
        obs = (await session.execute(select(ReconcileObservationRow).where(
            ReconcileObservationRow.account_id == "a"))).scalars().all()

    assert ps.realized_usdt == Decimal("450")        # live view overwritten
    assert ps.n_credits == 3
    assert ps.last_event_seq == 1                     # fence = current head (the seeded row's seq)
    assert len(obs) == 1                              # append-only checkpoint written
    assert obs[0].realized_usdt == Decimal("450")
    assert obs[0].event_seq_fence == ps.last_event_seq
    assert drift.realized_drift == Decimal("150")     # |450 - 300|
    assert drift.reserved_drift == Decimal("0")
    await engine.dispose()
