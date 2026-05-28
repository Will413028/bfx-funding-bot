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


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_rebuild_uses_checkpoint_then_replays_only_tail():
    """A checkpoint sets realized=450 at fence=3. A later ORDER_FILL at seq=4
    (size=50) is the only tail event → rebuilt realized = 450 + 50 = 500.
    Pre-fence events must NOT be re-folded (would double-count to >500)."""
    engine, sm = await _engine()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        # pre-fence noise that the checkpoint already subsumes
        for seq, etype, size in [
            (1, "RESERVATION_CLAIMED", 150),
            (2, "ORDER_FILL", 150),
            (3, "ORDER_FILL", 150),
        ]:
            session.add(EventLogRow(
                account_id="a", deployment_environment="ci", event_type=etype,
                cid=seq, venue_offer_id=f"v{seq}", venue_seq=seq,
                payload={"size_usdt": str(size), "cid": seq, "venue_offer_id": f"v{seq}",
                         "credit_id": None, "fill_rate": 0.0, "reason": "x",
                         "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
                         "account_id": "a", "is_simulated": False},
                occurred_at_ms=seq))
        # checkpoint at fence=3: absolute venue truth realized=450
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, observed_at_ms=100, event_seq_fence=3))
        # tail: one fill AFTER the fence
        session.add(EventLogRow(
            account_id="a", deployment_environment="ci", event_type="ORDER_FILL",
            cid=4, venue_offer_id="v4", venue_seq=4,
            payload={"size_usdt": "50", "cid": 4, "venue_offer_id": "v4",
                     "credit_id": None, "fill_rate": 0.0,
                     "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
                     "account_id": "a", "is_simulated": False},
            occurred_at_ms=200))
        await session.commit()

        await store.rebuild_snapshot_from_log(
            session, account_id="a", deployment_environment="ci")
        await session.commit()

        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalar_one()

    assert ps.realized_usdt == Decimal("500")   # 450 checkpoint + 50 tail
    assert ps.reserved_usdt == Decimal("0")
    await engine.dispose()
