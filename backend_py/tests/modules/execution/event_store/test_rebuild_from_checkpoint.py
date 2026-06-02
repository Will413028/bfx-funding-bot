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
                payload={"amount": str(size), "size_usdt": str(size), "symbol": "fUST",
                         "cid": seq, "venue_offer_id": f"v{seq}",
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
            payload={"amount": "50", "size_usdt": "50", "symbol": "fUST",
                     "cid": 4, "venue_offer_id": "v4",
                     "credit_id": None, "fill_rate": 0.0,
                     "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
                     "account_id": "a", "is_simulated": False},
            occurred_at_ms=200))
        await session.commit()

        await store.rebuild_snapshot_from_log(
            session, account_id="a", deployment_environment="ci", symbol="fUST")
        await session.commit()

        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalar_one()

    assert ps.realized == Decimal("500")   # 450 checkpoint + 50 tail
    assert ps.reserved == Decimal("0")
    await engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_seeds_from_same_symbol_checkpoint():
    """Two checkpoints exist: fUST (fence=3, realized=450) then a LATER fUSD
    (fence=4, realized=200). Rebuilding fUST must seed from the fUST checkpoint
    (450), not the newer fUSD one (200). Proves the checkpoint base is per-symbol."""
    engine, sm = await _engine()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci", symbol="fUST",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, observed_at_ms=100, event_seq_fence=3))
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci", symbol="fUSD",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("200"),
            n_offers=0, n_credits=1, observed_at_ms=200, event_seq_fence=4))
        await session.commit()
        await store.rebuild_snapshot_from_log(
            session, account_id="a", deployment_environment="ci", symbol="fUST")
        await session.commit()
        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a",
            PositionStateRow.symbol == "fUST"))).scalar_one()
    assert ps.realized == Decimal("450")   # seeded from fUST checkpoint, not fUSD's 200
    await engine.dispose()


def _claimed_payload(*, symbol: str, amount: Decimal, seq: int) -> dict:
    """A RESERVATION_CLAIMED payload as serialization.py persists it: carries
    BOTH `amount` (canonical) and `symbol`. Deliberately omits `size_usdt` so
    this test fails if the fold still reads the legacy field (would see 0)."""
    return {
        "amount": str(amount),
        "symbol": symbol,
        "cid": seq,
        "venue_offer_id": f"v{seq}",
        "credit_id": None,
        "fill_rate": 0.0,
        "reason": "x",
        "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
        "account_id": "default",
        "is_simulated": False,
    }


@pytest.mark.asyncio
async def test_rebuild_folds_two_symbols_into_two_rows():
    """Two currencies coexist in one event_log. The tail fold MUST filter by
    payload['symbol'] so fUST and fUSD deltas land in separate position_state
    rows (fUST=100, NOT 140). Also proves the fold reads payload['amount'] (the
    crafted payloads carry no size_usdt — a legacy fold would compute 0)."""
    engine, sm = await _engine()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        session.add(EventLogRow(
            account_id="default", deployment_environment="ci",
            event_type="RESERVATION_CLAIMED", cid=1, venue_offer_id="v1", venue_seq=1,
            payload=_claimed_payload(symbol="fUST", amount=Decimal("100"), seq=1),
            occurred_at_ms=1))
        session.add(EventLogRow(
            account_id="default", deployment_environment="ci",
            event_type="RESERVATION_CLAIMED", cid=2, venue_offer_id="v2", venue_seq=2,
            payload=_claimed_payload(symbol="fUSD", amount=Decimal("40"), seq=2),
            occurred_at_ms=2))
        await session.commit()

        await store.rebuild_snapshot_from_log(
            session, account_id="default", deployment_environment="ci", symbol="fUST")
        await store.rebuild_snapshot_from_log(
            session, account_id="default", deployment_environment="ci", symbol="fUSD")
        await session.commit()

        rows = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "default",
            PositionStateRow.deployment_environment == "ci"))).scalars().all()
    by_symbol = {r.symbol: r for r in rows}
    assert by_symbol["fUST"].reserved == Decimal("100")   # NOT 140 — fold filtered by symbol
    assert by_symbol["fUSD"].reserved == Decimal("40")
    await engine.dispose()
