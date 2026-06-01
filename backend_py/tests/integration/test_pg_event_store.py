"""Integration tests for PostgresEventStore — position_state snapshot maintenance.

Requires real Postgres (testcontainers). Run with:
    cd backend_py && uv run pytest tests/integration/test_pg_event_store.py -q -m integration
"""
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry, RegistryState

pytestmark = pytest.mark.integration
_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def test_position_state_reserved_realized(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    async with pg_session_factory() as s:
        await store.append(
            s,
            ReservationClaimed(
                cid=1,
                venue_offer_id="v1",
                size_usdt=Decimal("10"),
                signal_correlation_id=_SCID,
                account_id="acct",
                is_simulated=True,
                venue_seq=1,
                occurred_at_ms=1000,
            symbol="fUST"),
        )
        await store.append(
            s,
            OrderFilled(
                cid=1,
                venue_offer_id="v1",
                credit_id="c1",
                size_usdt=Decimal("4"),
                fill_rate=0.0,
                signal_correlation_id=_SCID,
                account_id="acct",
                is_simulated=True,
                venue_seq=2,
                occurred_at_ms=2000,
            symbol="fUST"),
        )
        await s.commit()
    async with pg_session_factory() as s:
        row = (
            await s.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == "acct",
                    PositionStateRow.symbol == "fUST",
                )
            )
        ).scalar_one()
        assert row.reserved == Decimal("6")  # 10 - 4
        assert row.realized == Decimal("4")
        assert row.last_event_seq > 0


async def test_fill_redelivery_does_not_double_count(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    fill = OrderFilled(
        cid=2,
        venue_offer_id="v2",
        credit_id=None,
        size_usdt=Decimal("3"),
        fill_rate=0.0,
        signal_correlation_id=_SCID,
        account_id="acct2",
        is_simulated=True,
        venue_seq=5,
        occurred_at_ms=2000,
    symbol="fUST")
    async with pg_session_factory() as s:
        await store.append(
            s,
            ReservationClaimed(
                cid=2,
                venue_offer_id="v2",
                size_usdt=Decimal("3"),
                signal_correlation_id=_SCID,
                account_id="acct2",
                is_simulated=True,
                venue_seq=4,
                occurred_at_ms=1000,
            symbol="fUST"),
        )
        await store.append(s, fill)
        await store.append(s, fill)  # duplicate — must be deduped
        await s.commit()
    async with pg_session_factory() as s:
        row = (
            await s.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == "acct2",
                    PositionStateRow.symbol == "fUST",
                )
            )
        ).scalar_one()
        assert row.realized == Decimal("3")  # not 6


async def test_ledger_from_snapshot(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    async with pg_session_factory() as s:
        await store.append(s, ReservationClaimed(cid=10, venue_offer_id="v10",
            size_usdt=Decimal("7"), signal_correlation_id=_SCID, account_id="snapA",
            is_simulated=True, venue_seq=1, occurred_at_ms=1000, symbol="fUST"))
        await s.commit()
    async with pg_session_factory() as s:
        ledger = await PaperPositionLedger.from_snapshot(s, account_id="snapA",
                                                         deployment_environment="ci")
    assert ledger.current_exposure("fUST") == Decimal("7")


async def test_registry_from_snapshot(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    async with pg_session_factory() as s:
        await store.append(s, ReservationClaimed(cid=11, venue_offer_id="v11",
            size_usdt=Decimal("1"), signal_correlation_id=_SCID, account_id="snapB",
            is_simulated=True, venue_seq=1, occurred_at_ms=1000, symbol="fUST"))
        await s.commit()
    async with pg_session_factory() as s:
        reg = await OfferRegistry.from_snapshot(s, account_id="snapB",
                                                deployment_environment="ci")
    snap = reg.snapshot()
    assert "v11" in snap
    assert snap["v11"].state is RegistryState.CLAIMED


async def test_rebuild_matches_incremental(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    # Use cids 101/102 to avoid collision with other tests (cid is the upsert PK).
    events = [
        ReservationClaimed(cid=101, venue_offer_id="ra", size_usdt=Decimal("10"),
            signal_correlation_id=_SCID, account_id="R", is_simulated=True,
            venue_seq=1, occurred_at_ms=1000, symbol="fUST"),
        ReservationClaimed(cid=102, venue_offer_id="rb", size_usdt=Decimal("5"),
            signal_correlation_id=_SCID, account_id="R", is_simulated=True,
            venue_seq=2, occurred_at_ms=1100, symbol="fUST"),
        OrderFilled(cid=101, venue_offer_id="ra", credit_id="c", size_usdt=Decimal("4"),
            fill_rate=0.0, signal_correlation_id=_SCID, account_id="R", is_simulated=True,
            venue_seq=3, occurred_at_ms=1200, symbol="fUST"),
        ReservationReleased(cid=102, venue_offer_id="rb", size_usdt=Decimal("5"),
            reason="venue_cancel", signal_correlation_id=_SCID, account_id="R",
            is_simulated=True, venue_seq=4, occurred_at_ms=1300, symbol="fUST"),
    ]
    async def _claims(session) -> dict[int, tuple[str, str | None, int]]:  # type: ignore[type-arg]
        rows = (await session.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == "R"))).scalars().all()
        return {r.cid: (r.state, r.venue_offer_id, r.last_updated_ms) for r in rows}

    async with pg_session_factory() as s:
        for e in events:
            await store.append(s, e)
        await s.commit()
    async with pg_session_factory() as s:
        incr = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "R",
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        incr_reserved, incr_realized = Decimal(str(incr.reserved)), Decimal(str(incr.realized))
        incr_claims = await _claims(s)
    async with pg_session_factory() as s:
        await store.rebuild_snapshot_from_log(s, account_id="R", deployment_environment="ci", symbol="fUST")
        await s.commit()
    async with pg_session_factory() as s:
        rb = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "R",
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        assert Decimal(str(rb.reserved)) == incr_reserved == Decimal("6")
        assert Decimal(str(rb.realized)) == incr_realized == Decimal("4")
        # offer_claims must also match incremental vs rebuild
        rb_claims = await _claims(s)
        assert len(incr_claims) == 2
        assert rb_claims == incr_claims
