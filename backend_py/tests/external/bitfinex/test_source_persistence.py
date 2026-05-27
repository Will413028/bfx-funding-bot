"""WS/fill-tracker source persistence — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration
"""
import asyncio
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.external.bitfinex.auth_ws import FocEvent
from bfx_funding_bot.external.bitfinex.fill_tracker import RestPollingFillTracker
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
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName

pytestmark = pytest.mark.integration
_ENV = "ci"
_ACC = "src_ws"


class _StubWSClient:
    def __init__(self, events):
        self._events = events
    async def events(self):
        for e in self._events:
            yield e


class _EventCapture:
    async def emit(self, event):
        return None


def _registry_with_claim(voi, cid, scid, size, account_id=_ACC):
    reg = OfferRegistry(clock=lambda: 0)
    reg._snapshot = {voi: ClaimRecord(
        venue_offer_id=voi, cid=cid, signal_correlation_id=scid,
        size_usdt=Decimal(str(size)), account_id=account_id, state=RegistryState.CLAIMED,
        occurred_at_ms=0, last_updated_ms=0,
    )}
    return reg


@pytest.mark.asyncio
async def test_ws_foc_executed_orderfilled_persisted_before_publish(pg_session_factory):
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
    foc = FocEvent(venue_offer_id="888", symbol="fUSD", mts_create=10, mts_update=10,
                   amount=Decimal("100"), status="EXECUTED @ 0.0003 (100.0)",
                   rate=0.0003, period_days=2, raw_seq=99, raw=[])
    bus = DomainEventBus()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_StubWSClient([foc]),
        registry=_registry_with_claim("888", 11, scid, 100),
        bus=bus, event_sink=_EventCapture(), persister=persister,
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


_ACC_FT = "src_ft"


class _OneTickHttp:
    """First /offers poll returns one offer (voi=888), then it disappears."""
    def __init__(self):
        self._calls = 0

    async def get(self, path):
        resp = MagicMock()
        resp.status_code = 200
        if path.endswith("/credits"):
            resp.json = lambda: []
            return resp
        self._calls += 1
        if self._calls == 1:
            row = [None] * 21
            row[0] = 888
            row[5] = -100.0
            row[20] = 11  # legacy cid slot (fill_tracker reads o[20]); not used by recovery
            resp.json = lambda: [row]
        else:
            resp.json = lambda: []
        return resp


@pytest.mark.asyncio
async def test_fill_tracker_release_persisted_before_publish(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=11, size_usdt=Decimal("100"), signal_correlation_id=scid,
                         account_id=_ACC_FT, is_simulated=False, occurred_at_ms=1),
        ReservationClaimed(cid=11, venue_offer_id="888", size_usdt=Decimal("100"),
                          signal_correlation_id=scid, account_id=_ACC_FT, is_simulated=False,
                          occurred_at_ms=2),
    )
    tracker = RestPollingFillTracker(
        http=_OneTickHttp(), event_sink=_EventCapture(), probe=HealthProbe(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
        account_id=_ACC_FT,
        registry=_registry_with_claim("888", 11, scid, 100, account_id=_ACC_FT),
        persister=persister, poll_interval_s=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(tracker.poll_loop(stop))
    await asyncio.sleep(0.25)  # let >=2 ticks run (present -> gone)
    stop.set()
    await task

    async with pg_session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_RELEASED",
            EventLogRow.account_id == _ACC_FT))).scalar_one()
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == _ACC_FT,
            PositionStateRow.deployment_environment == _ENV))).scalar_one()
    assert n == 1
    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")


class _FlakyPersister:
    """Fails the first persist call, then delegates to the real persister."""
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
    async def persist(self, *events):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient pg down")
        await self._inner.persist(*events)


@pytest.mark.asyncio
async def test_fill_tracker_release_retried_after_persist_failure(pg_session_factory):
    acc = "src_ft_retry"
    store = PostgresEventStore(deployment_environment=_ENV)
    real = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    await real.persist(
        ReservationIntent(cid=12, size_usdt=Decimal("70"), signal_correlation_id=scid,
                         account_id=acc, is_simulated=False, occurred_at_ms=1),
        ReservationClaimed(cid=12, venue_offer_id="889", size_usdt=Decimal("70"),
                          signal_correlation_id=scid, account_id=acc, is_simulated=False,
                          occurred_at_ms=2),
    )

    class _GoneAfterFirst:
        """Offer 889 present on tick 1, gone on every tick after."""
        def __init__(self):
            self._calls = 0
        async def get(self, path):
            resp = MagicMock()
            resp.status_code = 200
            if path.endswith("/credits"):
                resp.json = lambda: []
                return resp
            self._calls += 1
            if self._calls == 1:
                row = [None] * 21
                row[0] = 889
                row[5] = -70.0
                row[20] = 12
                resp.json = lambda: [row]
            else:
                resp.json = lambda: []
            return resp

    flaky = _FlakyPersister(real)
    tracker = RestPollingFillTracker(
        http=_GoneAfterFirst(), event_sink=_EventCapture(), probe=HealthProbe(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
        account_id=acc, registry=_registry_with_claim("889", 12, scid, 70, account_id=acc),
        persister=flaky, poll_interval_s=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(tracker.poll_loop(stop))
    await asyncio.sleep(0.4)  # tick1: present; tick2: gone -> persist fails (retained);
                              # tick3: gone -> persist succeeds
    stop.set()
    await task

    assert flaky.calls >= 2  # failed once, then retried
    async with pg_session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_RELEASED",
            EventLogRow.account_id == acc))).scalar_one()
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == acc,
            PositionStateRow.deployment_environment == _ENV))).scalar_one()
    assert n == 1            # exactly one release persisted after retry (no dup)
    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")
