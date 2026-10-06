"""The WS dispatcher's run loop, cancel tracking and queue accessors, on a venue hint sink.

The dispatcher translates nothing into capital events: each closing offer or credit becomes
an untrusted hint for the injected ``VenueHintSink`` (the ledger's, in the bot).
"""
import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import BfxWSEvent, FcnEvent, FocEvent
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import CancelRequested
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import (
    CreditCloseHint,
    OfferCloseHint,
    Scope,
    VenueHintNotification,
)
from bfx_funding_bot.modules.ledger.wiring import build_venue_hint_sink

SCOPE = Scope(UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"), "ci")


class _FakeWSClient:
    """Fake BitfinexAuthWSClient — emits scripted BfxWSEvent stream, then holds open."""

    def __init__(self, events: list[BfxWSEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[BfxWSEvent]:
        for ev in self._events:
            yield ev
        await asyncio.Event().wait()


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


class _RecordingSink:
    """A VenueHintSink that records each hint and signals the first one."""

    def __init__(self) -> None:
        self.offers: list[OfferCloseHint] = []
        self.credits: list[CreditCloseHint] = []
        self.received = asyncio.Event()

    async def offer_closed(self, hint: OfferCloseHint) -> None:
        self.offers.append(hint)
        self.received.set()

    async def credit_closed(self, hint: CreditCloseHint) -> None:
        self.credits.append(hint)
        self.received.set()

    async def offer_gone(self, venue_offer_id: str, *, occurred_at_ms: int) -> bool:
        return True


def _foc(voi: str, status: str, raw_seq: int) -> FocEvent:
    return FocEvent(
        venue_offer_id=voi, symbol="fUSD", mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status=status, rate=0.0005, period_days=2,
        raw_seq=raw_seq, raw=[],
    )


async def _run_until_received(dispatcher: BitfinexLiveWSDispatcher, sink: _RecordingSink) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    try:
        await asyncio.wait_for(sink.received.wait(), timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_run_hands_a_closing_offer_from_the_stream_to_the_sink() -> None:
    sink = _RecordingSink()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_FakeWSClient([_foc("42", "EXECUTED @ 0.0005 (100.0)", 5)]),
        event_sink=_EventCapture(), clock=lambda: 5000, queue_max=100, venue_hint_sink=sink)

    await _run_until_received(dispatcher, sink)

    assert sink.offers == [OfferCloseHint(
        venue_offer_id="42", symbol="fUSD", kind="EXECUTED @ 0.0005 (100.0)", rate=0.0005,
        occurred_at_ms=2000, venue_seq=5, received_at_ms=5000, cancel_requested_at_ms=None,
    )]
    assert sink.credits == []


@pytest.mark.asyncio
async def test_a_cancel_requested_on_the_bus_rides_on_the_closing_hint() -> None:
    bus = DomainEventBus()
    sink = _RecordingSink()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_FakeWSClient([_foc("42", "CANCELED", 7)]),
        event_sink=_EventCapture(), clock=lambda: 2200, queue_max=100, venue_hint_sink=sink)
    bus.subscribe(CancelRequested, dispatcher.handle_cancel_requested)
    await bus.publish(CancelRequested(
        venue_offer_id="42", requested_at_ms=2000,
        signal_correlation_id=uuid4(), account_id="default",
    ))

    await _run_until_received(dispatcher, sink)

    assert [(h.venue_offer_id, h.kind, h.cancel_requested_at_ms) for h in sink.offers] == [
        ("42", "CANCELED", 2000)]


@pytest.mark.asyncio
async def test_a_foreign_offer_closing_only_asks_the_ledger_to_reconcile() -> None:
    # Regression: cancelling a foreign offer (no claim) once raised out of the TaskGroup
    # and stopped the bot. Under the ledger it is a hint: a resync request and a
    # notification, never a capital event.
    published: list[object] = []

    class _CapturingBus(DomainEventBus):
        """Records every publication whatever its type."""

        async def publish(self, event: object) -> None:
            published.append(event)
            await super().publish(event)

    bus = _CapturingBus()
    requests: list[str] = []
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_FakeWSClient([]), event_sink=_EventCapture(), clock=lambda: 5000,
        venue_hint_sink=build_venue_hint_sink(
            scope=SCOPE, request_resync=requests.append, bus=bus, monotonic=lambda: 0.0))

    await dispatcher._process(_foc("foreign", "CANCELED", 8))

    assert requests == ["venue_hint:offer_closed:foreign"]
    assert [type(event) for event in published] == [VenueHintNotification]


@pytest.mark.asyncio
async def test_a_credit_notification_is_informational() -> None:
    """fcn carries no offer id and closes nothing: no hint, before or after any offer event."""
    sink = _RecordingSink()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_FakeWSClient([]), event_sink=_EventCapture(), clock=lambda: 5000,
        venue_hint_sink=sink)
    fcn = FcnEvent(
        credit_id=81, symbol="fUSD", side=1, mts_create=1000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2, raw_seq=3, raw=[],
    )

    await dispatcher._process(fcn)
    await dispatcher._process(_foc("42", "EXECUTED @ 0.0005 (100.0)", 5))
    await dispatcher._process(fcn)

    assert [hint.venue_offer_id for hint in sink.offers] == ["42"]
    assert sink.credits == []


@pytest.mark.asyncio
async def test_dispatcher_queue_observability_accessors() -> None:
    """queue_depth / queue_capacity are read-only observability accessors for
    the Prometheus saturation gauges (bfx_ws_dispatcher_queue_*). No behavior."""
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_FakeWSClient([]), event_sink=_EventCapture(), queue_max=77,
        venue_hint_sink=_RecordingSink())
    assert dispatcher.queue_depth == 0
    assert dispatcher.queue_capacity == 77
    dispatcher._queue.put_nowait(object())  # type: ignore[arg-type]
    assert dispatcher.queue_depth == 1
