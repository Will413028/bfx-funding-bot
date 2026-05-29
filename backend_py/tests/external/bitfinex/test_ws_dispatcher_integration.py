import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import BfxWSEvent, FocEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    OfferRegistry,
    RegistryState,
)


class _FakeWSClient:
    """Fake BitfinexAuthWSClient — emits scripted BfxWSEvent stream."""
    def __init__(self, events: list[BfxWSEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[BfxWSEvent]:
        for ev in self._events:
            yield ev
            await asyncio.sleep(0.001)
        # hold open
        while True:
            await asyncio.sleep(0.1)


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.asyncio
async def test_dispatcher_publishes_orderfilled_on_foc_executed() -> None:
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)
    bus.subscribe(ReservationClaimed, registry.handle)
    bus.subscribe(OrderFilled, registry.handle)
    bus.subscribe(ReservationReleased, registry.handle)

    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    ))

    foc = FocEvent(
        venue_offer_id="42", symbol="fUSD",
        mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="EXECUTED @ 0.0005 (100.0)",
        rate=0.0005, period_days=2, raw_seq=5, raw=[],
    )
    fake_ws = _FakeWSClient([foc])

    captured: list = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)
    bus.subscribe(OrderFilled, capture)

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 5000, queue_max=100,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    assert len(captured) == 1
    assert captured[0].credit_id is None
    assert captured[0].venue_offer_id == "42"
    assert captured[0].fill_rate == 0.0005


@pytest.mark.asyncio
async def test_dispatcher_cancel_requested_subscriber_tracks_recent_cancels() -> None:
    bus = DomainEventBus(clock=lambda: 2200)
    registry = OfferRegistry(clock=lambda: 2200)
    bus.subscribe(ReservationClaimed, registry.handle)
    bus.subscribe(ReservationReleased, registry.handle)

    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    ))

    captured: list = []
    async def capture(ev: ReservationReleased) -> None:
        captured.append(ev)
    bus.subscribe(ReservationReleased, capture)

    foc = FocEvent(
        venue_offer_id="42", symbol="fUSD",
        mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="CANCELED",
        rate=0.0005, period_days=2, raw_seq=7, raw=[],
    )
    fake_ws = _FakeWSClient([foc])

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 2200, queue_max=100,
    )
    bus.subscribe(CancelRequested, dispatcher.handle_cancel_requested)

    # Publish CancelRequested first (cancel @ 2000, dispatcher clock 2200 → δ=200ms ≤ 5000)
    await bus.publish(CancelRequested(
        venue_offer_id="42", requested_at_ms=2000,
        signal_correlation_id=uuid4(), account_id="default",
    ))

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    assert len(captured) == 1
    assert captured[0].reason == "user_cancel"


# ---------------------------------------------------------------------------
# Audit I1: dedup-aware _persist_then_publish — skip publish on dedup
# ---------------------------------------------------------------------------

class _FakePersister:
    """Fake EventPersister that simulates persist returning per-event dedup status."""

    def __init__(self, statuses: list[list[bool]]) -> None:
        """statuses: per-call list; each element is the list[bool] returned for that call."""
        self._statuses = list(statuses)
        self.calls: list[tuple[object, ...]] = []

    async def persist(self, *events: object) -> list[bool]:
        self.calls.append(events)
        return self._statuses.pop(0) if self._statuses else [True] * len(events)


def _registry_with_claim(voi: str) -> OfferRegistry:
    reg = OfferRegistry(clock=lambda: 5000)
    reg._snapshot = {
        voi: ClaimRecord(
            venue_offer_id=voi,
            cid=42,
            signal_correlation_id=uuid4(),
            size_usdt=Decimal("100"),
            account_id="default",
            state=RegistryState.CLAIMED,
            occurred_at_ms=1000,
            last_updated_ms=1000,
        )
    }
    return reg


def _foc_executed(voi: str = "v1", raw_seq: int = 5) -> FocEvent:
    return FocEvent(
        venue_offer_id=voi,
        symbol="fUSD",
        mts_create=1000,
        mts_update=2000,
        amount=Decimal("100"),
        status="EXECUTED @ 0.0005 (100.0)",
        rate=0.0005,
        period_days=2,
        raw_seq=raw_seq,
        raw=[],
    )


@pytest.mark.asyncio
async def test_deduped_event_not_published_to_bus() -> None:
    """Audit I1: when persist() returns [False] (dedup), bus.publish is NOT called."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = _registry_with_claim("v1")

    published: list[Any] = []

    async def capture(ev: Any) -> None:
        published.append(ev)

    bus.subscribe(OrderFilled, capture)

    # Persist returns False — simulating WS re-delivery of already-stored event.
    fake_persister = _FakePersister(statuses=[[False]])

    fake_ws = _FakeWSClient([_foc_executed("v1", raw_seq=5)])
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws,
        registry=registry,
        bus=bus,
        event_sink=_EventCapture(),
        clock=lambda: 5000,
        queue_max=100,
        persister=fake_persister,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    # persist was called once
    assert len(fake_persister.calls) == 1
    # but bus.publish was NOT called because persist returned False (dedup)
    assert published == [], f"expected no publish, got {published}"


@pytest.mark.asyncio
async def test_persisted_event_is_published_to_bus() -> None:
    """Audit I1 (positive path): persist() returns [True] → bus.publish IS called."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = _registry_with_claim("v2")

    published: list[Any] = []

    async def capture(ev: Any) -> None:
        published.append(ev)

    bus.subscribe(OrderFilled, capture)

    # Persist returns True — new event, should publish.
    fake_persister = _FakePersister(statuses=[[True]])

    fake_ws = _FakeWSClient([_foc_executed("v2", raw_seq=7)])
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws,
        registry=registry,
        bus=bus,
        event_sink=_EventCapture(),
        clock=lambda: 5000,
        queue_max=100,
        persister=fake_persister,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    assert len(fake_persister.calls) == 1
    assert len(published) == 1
    assert isinstance(published[0], OrderFilled)
