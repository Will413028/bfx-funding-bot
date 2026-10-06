"""The frozen legacy projection of ``VENUE_OFFER_QUARANTINED`` events, on PostgreSQL.

Since lending envelope D2 an offer no durable intent traces to is foreign, and since S1-8
no runtime writes the legacy event log at all. Breadcrumbs recorded before that still
replay (the DR prefix verification rebuilds the projection from the frozen log), so the
event store keeps its own regressions here. The history is planted with the event store's
writer, as the legacy runtime appended it.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, VenueOfferStateRow
from bfx_funding_bot.modules.execution.event_store.writer import ProjectionWriteError
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    SnapshotCoverage,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

from .legacy_history import append_legacy

pytestmark = pytest.mark.integration

_ENV = "ci"
_ACCOUNT = UUID("00000000-0000-0000-0000-000000000042")


def _observed(venue_offer_id: str, symbol: str, amount: str, remaining: str, status: str, *,
              at: int) -> VenueSnapshotObserved:
    return VenueSnapshotObserved(
        account_id=str(_ACCOUNT), environment=_ENV,
        query_started_at_ms=at - 100, query_finished_at_ms=at,
        offers=(VenueOfferObservation(
            venue_offer_id=venue_offer_id, symbol=symbol, amount_original=Decimal(amount),
            amount_remaining=Decimal(remaining), rate=Decimal("0.0003"), period_days=2,
            status=status, mts_created=1_000, mts_updated=at,
        ),),
        credits=(), wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(True, True, True),
    )


async def _seed_account_and_known_claim(pg_session_factory) -> None:
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="orphan"))
        await session.commit()
    signal_id = uuid4()
    await append_legacy(pg_session_factory, ReservationClaimed(
        symbol="fUST", cid=42, venue_offer_id="known", signal_correlation_id=signal_id,
        account_id=str(_ACCOUNT), is_simulated=False, amount=Decimal("40"), occurred_at_ms=900,
        reservation_ref=ReservationRef(
            execution_decision_id="known-decision", cid=42, signal_correlation_id=signal_id,
            venue_offer_id="known",
        ),
    ), compatibility_mode=True)


@pytest.mark.asyncio
async def test_quarantine_rebuild_preserves_fk_and_old_snapshot_cannot_reopen_terminal_offer(
    pg_session_factory,
):
    """Replay order and a delayed observation must preserve terminal monotonicity."""
    await _seed_account_and_known_claim(pg_session_factory)
    store = PostgresEventStore(deployment_environment=_ENV)
    # What the legacy reconcile recorded: the orphan observed live, then its breadcrumb.
    async with pg_session_factory() as session:
        await store.append_snapshot(session, _observed("orphan", "fXYZ", "7", "7", "ACTIVE",
                                                       at=5_000))
        await session.commit()
    await append_legacy(pg_session_factory, VenueOfferQuarantined(
        venue_offer_id="orphan", symbol="fXYZ", amount=Decimal("7"), account_id=str(_ACCOUNT),
        observed_at_ms=5_000,
    ))

    terminal = _observed("orphan", "fXYZ", "7", "0", "CANCELED", at=6_000)
    delayed_active = _observed("orphan", "fXYZ", "7", "7", "ACTIVE", at=4_000)
    async with pg_session_factory() as session:
        await store.append_snapshot(session, terminal)
        await store.append_snapshot(session, delayed_active)
        await session.commit()
    async with pg_session_factory() as session:
        await store.rebuild_snapshot_from_log(
            session, account_id=str(_ACCOUNT), deployment_environment=_ENV,
        )
        await session.commit()

    async with pg_session_factory() as session:
        orphan = await session.get(VenueOfferStateRow, (_ACCOUNT, _ENV, "orphan"))
        uncertainty = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
    assert orphan is not None
    assert orphan.is_terminal is True
    assert orphan.status == "canceled"
    assert uncertainty.venue_offer_id == orphan.venue_offer_id
    assert uncertainty.opened_event_seq < orphan.last_seen_event_seq


@pytest.mark.asyncio
async def test_registered_account_standalone_quarantine_rolls_back(pg_session_factory):
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="standalone"))
        await session.commit()
    event = VenueOfferQuarantined(
        venue_offer_id="not-observed", symbol="fUST", amount=Decimal("5"),
        account_id=str(_ACCOUNT), observed_at_ms=2_000,
    )

    with pytest.raises(ProjectionWriteError, match="snapshot venue observation"):
        await append_legacy(pg_session_factory, event)

    async with pg_session_factory() as session:
        event_count = len((await session.execute(select(EventLogRow))).scalars().all())
        uncertainty_count = len(
            (await session.execute(select(ExecutionUncertaintyRow))).scalars().all()
        )
    assert event_count == 0
    assert uncertainty_count == 0
