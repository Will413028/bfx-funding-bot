"""Integration tests for PostgresEventStore — position_state snapshot maintenance.

Requires real Postgres (testcontainers). Run with:
    cd backend && uv run pytest tests/integration/test_pg_event_store.py -q -m integration
"""
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)

pytestmark = pytest.mark.integration
_SCID = UUID("11111111-1111-1111-1111-111111111111")
_ACCOUNT = "00000000-0000-0000-0000-000000000031"
_ACCOUNT_2 = "00000000-0000-0000-0000-000000000032"
_REBUILD_ACCOUNT = "00000000-0000-0000-0000-000000000035"


def make_reservation_ref(cid: int, signal_correlation_id: UUID, venue_offer_id: str) -> ReservationRef:
    """The explicit correlation a lifecycle event carries."""
    return ReservationRef(execution_decision_id=f"event-store-test-{cid}", cid=cid,
                          signal_correlation_id=signal_correlation_id, venue_offer_id=venue_offer_id)


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
                account_id=_ACCOUNT,
                is_simulated=True,
                venue_seq=1,
                occurred_at_ms=1000,
                symbol="fUST", reservation_ref=make_reservation_ref(1, _SCID, "v1"),
            ),
        )
        await store.append(
            s,
            ReservationClaimed(
                cid=3,
                venue_offer_id="v3",
                size_usdt=Decimal("6"),
                signal_correlation_id=_SCID,
                account_id=_ACCOUNT,
                is_simulated=True,
                venue_seq=3,
                occurred_at_ms=1500,
                symbol="fUST", reservation_ref=make_reservation_ref(3, _SCID, "v3"),
            ),
        )
        await store.append(
            s,
            OrderFilled(
                cid=1,
                venue_offer_id="v1",
                credit_id="c1",
                size_usdt=Decimal("10"),
                fill_rate=0.0,
                signal_correlation_id=_SCID,
                account_id=_ACCOUNT,
                is_simulated=True,
                venue_seq=2,
                occurred_at_ms=2000,
                symbol="fUST", reservation_ref=make_reservation_ref(1, _SCID, "v1"),
            ),
        )
        await s.commit()
    async with pg_session_factory() as s:
        row = (
            await s.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == _ACCOUNT,
                    PositionStateRow.symbol == "fUST",
                )
            )
        ).scalar_one()
        assert row.reserved == Decimal("6")  # second claim remains open
        assert row.realized == Decimal("10")
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
        account_id=_ACCOUNT_2,
        is_simulated=True,
        venue_seq=5,
        occurred_at_ms=2000,
        symbol="fUST", reservation_ref=make_reservation_ref(2, _SCID, "v2"),
    )
    async with pg_session_factory() as s:
        await store.append(
            s,
            ReservationClaimed(
                cid=2,
                venue_offer_id="v2",
                size_usdt=Decimal("3"),
                signal_correlation_id=_SCID,
                account_id=_ACCOUNT_2,
                is_simulated=True,
                venue_seq=4,
                occurred_at_ms=1000,
                symbol="fUST", reservation_ref=make_reservation_ref(2, _SCID, "v2"),
            ),
        )
        await store.append(s, fill)
        await store.append(s, fill)  # duplicate — must be deduped
        await s.commit()
    async with pg_session_factory() as s:
        row = (
            await s.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == _ACCOUNT_2,
                    PositionStateRow.symbol == "fUST",
                )
            )
        ).scalar_one()
        assert row.realized == Decimal("3")  # not 6


async def test_rebuild_matches_incremental(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    # Use cids 101/102 to avoid collision with other tests (cid is the upsert PK).
    events = [
        ReservationClaimed(cid=101, venue_offer_id="ra", size_usdt=Decimal("10"),
            signal_correlation_id=_SCID, account_id=_REBUILD_ACCOUNT, is_simulated=True,
            venue_seq=1, occurred_at_ms=1000, symbol="fUST",
            reservation_ref=make_reservation_ref(101, _SCID, "ra")),
        ReservationClaimed(cid=102, venue_offer_id="rb", size_usdt=Decimal("5"),
            signal_correlation_id=_SCID, account_id=_REBUILD_ACCOUNT, is_simulated=True,
            venue_seq=2, occurred_at_ms=1100, symbol="fUST",
            reservation_ref=make_reservation_ref(102, _SCID, "rb")),
        OrderFilled(cid=101, venue_offer_id="ra", credit_id="c", size_usdt=Decimal("10"),
            fill_rate=0.0, signal_correlation_id=_SCID, account_id=_REBUILD_ACCOUNT, is_simulated=True,
            venue_seq=3, occurred_at_ms=1200, symbol="fUST",
            reservation_ref=make_reservation_ref(101, _SCID, "ra")),
        ReservationReleased(cid=102, venue_offer_id="rb", size_usdt=Decimal("5"),
            reason="venue_cancel", signal_correlation_id=_SCID, account_id=_REBUILD_ACCOUNT,
            is_simulated=True, venue_seq=4, occurred_at_ms=1300, symbol="fUST",
            reservation_ref=make_reservation_ref(102, _SCID, "rb")),
    ]
    async def _claims(session) -> dict[int, tuple[str, str | None, int]]:  # type: ignore[type-arg]
        rows = (await session.execute(select(OfferClaimRow).where(
            OfferClaimRow.account_id == _REBUILD_ACCOUNT))).scalars().all()
        return {r.cid: (r.state, r.venue_offer_id, r.last_updated_ms) for r in rows}

    async with pg_session_factory() as s:
        for e in events:
            await store.append(s, e)
        await s.commit()
    async with pg_session_factory() as s:
        incr = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == _REBUILD_ACCOUNT,
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        incr_reserved, incr_realized = Decimal(str(incr.reserved)), Decimal(str(incr.realized))
        incr_claims = await _claims(s)
    async with pg_session_factory() as s:
        await store.rebuild_snapshot_from_log(s, account_id=_REBUILD_ACCOUNT, deployment_environment="ci", symbol="fUST")
        await s.commit()
    async with pg_session_factory() as s:
        rb = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == _REBUILD_ACCOUNT,
            PositionStateRow.symbol == "fUST",
        ))).scalar_one()
        assert Decimal(str(rb.reserved)) == incr_reserved == Decimal("0")
        assert Decimal(str(rb.realized)) == incr_realized == Decimal("10")
        # offer_claims must also match incremental vs rebuild
        rb_claims = await _claims(s)
        assert len(incr_claims) == 2
        assert rb_claims == incr_claims
