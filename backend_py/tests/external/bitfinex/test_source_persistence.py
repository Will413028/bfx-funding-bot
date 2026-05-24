"""WS/fill-tracker source persistence — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration
"""
import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.external.bitfinex.auth_ws import FcnEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationIntent
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    OfferRegistry,
    RegistryState,
)

pytestmark = pytest.mark.integration
_ENV = "ci"
_ACC = "src_ws"


class _StubWSClient:
    def __init__(self, events):
        self._events = events
    async def events(self):
        for e in self._events:
            yield e


class _NoopAxiom:
    async def emit(self, event):
        return None


def _registry_with_claim(voi, cid, scid, size):
    reg = OfferRegistry.__new__(OfferRegistry)
    reg._axiom_query = None  # type: ignore[attr-defined]
    reg._clock = lambda: 0
    reg._snapshot = {voi: ClaimRecord(
        venue_offer_id=voi, cid=cid, signal_correlation_id=scid,
        size_usdt=Decimal(str(size)), account_id=_ACC, state=RegistryState.CLAIMED,
        occurred_at_ms=0, last_updated_ms=0,
    )}
    return reg


@pytest.mark.asyncio
async def test_ws_fcn_orderfilled_persisted_before_publish(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    # seed a CLAIMED row so the offer is reserved (reserved=100)
    await persister.persist(
        ReservationIntent(cid=11, size_usdt=Decimal("100"), signal_correlation_id=scid,
                         account_id=_ACC, is_simulated=False, occurred_at_ms=1),
        ReservationClaimed(cid=11, venue_offer_id="888", size_usdt=Decimal("100"),
                          signal_correlation_id=scid, account_id=_ACC, is_simulated=False,
                          occurred_at_ms=2),
    )
    fcn = FcnEvent(credit_id=1, symbol="fUSD", side=1, mts_create=10, mts_update=10,
                   amount=Decimal("100"), rate=0.0003, period_days=2,
                   offer_id_meta=888, raw_seq=99)
    bus = DomainEventBus()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_StubWSClient([fcn]),
        registry=_registry_with_claim("888", 11, scid, 100),
        bus=bus, axiom=_NoopAxiom(), persister=persister,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await task

    async with pg_session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "ORDER_FILL",
            EventLogRow.account_id == _ACC))).scalar_one()
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == _ACC,
            PositionStateRow.deployment_environment == _ENV))).scalar_one()
    assert n == 1
    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")    # reserved -= 100
    assert Decimal(str(ps.realized_usdt)) == Decimal("100")  # realized += 100
