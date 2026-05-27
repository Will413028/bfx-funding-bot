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
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry


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
