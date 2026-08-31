"""Integration test: PG cutover boot round-trip (persist → from_snapshot, no Axiom).

Regression guard for the cold-start contract:
  events persisted via EventStorePersister must be fully reflected by a fresh
  PaperPositionLedger.from_snapshot + OfferRegistry.from_snapshot call on
  real Postgres — the daemon's new boot path introduced in Phase 4.4b.
"""
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry, RegistryState

from .conftest import make_reservation_ref

pytestmark = pytest.mark.integration
_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def test_sink_then_from_snapshot_roundtrip(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)

    await persister.persist(ReservationClaimed(
        cid=7, venue_offer_id="v7", size_usdt=Decimal("12"), signal_correlation_id=_SCID,
        account_id="00000000-0000-0000-0000-000000000025", is_simulated=True, venue_seq=1, occurred_at_ms=1000,
        symbol="fUST", reservation_ref=make_reservation_ref(7, _SCID, "v7")))
    await persister.persist(OrderFilled(
        cid=7, venue_offer_id="v7", credit_id="c7", size_usdt=Decimal("12"), fill_rate=0.0,
        signal_correlation_id=_SCID, account_id="00000000-0000-0000-0000-000000000025", is_simulated=True,
        venue_seq=2, occurred_at_ms=2000, symbol="fUST",
        reservation_ref=make_reservation_ref(7, _SCID, "v7")))

    async with pg_session_factory() as s:
        ledger = await PaperPositionLedger.from_snapshot(
            s, account_id="00000000-0000-0000-0000-000000000025", deployment_environment="ci")
        reg = await OfferRegistry.from_snapshot(
            s, account_id="00000000-0000-0000-0000-000000000025", deployment_environment="ci")

    assert ledger.current_exposure("fUST") == Decimal("12")   # full reservation is realized
    assert ledger.realized_exposure("fUST") == Decimal("12")
    # OrderFilled transitions CLAIMED → RELEASED in the FSM (offer is closed once filled)
    assert reg.snapshot()["v7"].state is RegistryState.RELEASED
