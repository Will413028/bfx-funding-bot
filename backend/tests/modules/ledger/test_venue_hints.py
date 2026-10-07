"""Ledger venue hint sink contract: no persistence capability, one resync per window."""

from dataclasses import asdict, replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import (
    CreditCloseHint,
    OfferCloseHint,
    Scope,
    VenueHintNotification,
)
from bfx_funding_bot.modules.ledger.wiring import build_venue_hint_sink

SCOPE = Scope(UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"), "ci")
OFFER = OfferCloseHint("42", "fUST", "EXECUTED", 0.0005, 8000, 17, 9000)
CREDIT = CreditCloseHint(81, "fUST", Decimal("100.00"), 0.0005, 2, 1000, 8000, 17)
KINDS = ("offer_closed", "credit_closed")


async def send(sink, kind):
    if kind == "offer_closed":
        await sink.offer_closed(OFFER)
    else:
        await sink.credit_closed(CREDIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_ledger_hint_requests_resync_once_per_window(kind):
    now = [100.0]
    requests = []
    bus = DomainEventBus()
    notifications = []

    async def capture(event):
        notifications.append(event)

    bus.subscribe(VenueHintNotification, capture)
    sink = build_venue_hint_sink(
        scope=SCOPE, request_resync=requests.append, bus=bus, monotonic=lambda: now[0],
    )
    await send(sink, kind)
    now[0] = 104.999
    await send(sink, kind)
    assert requests == [f"venue_hint:{kind}:{81 if kind == 'credit_closed' else 42}"]
    assert len(notifications) == 2  # only resync is debounced
    now[0] = 105.0
    await send(sink, kind)
    assert len(requests) == 2
    assert len(notifications) == 3
    for notification in notifications:
        assert notification.scope == SCOPE
        assert notification.kind == kind
        assert notification.occurred_at_ms == 8000
        assert notification.venue_offer_id == (None if kind == "credit_closed" else "42")
        assert notification.credit_id == (81 if kind == "credit_closed" else None)
        assert notification.venue_seq == 17
        assert notification.status == ("EXECUTED" if kind == "offer_closed" else None)
        assert "cid" not in asdict(notification)
        assert "attempt_id" not in asdict(notification)


@pytest.mark.asyncio
async def test_debounce_is_by_kind_and_venue_id_not_sequence_or_venue_time():
    requests = []
    bus = SimpleNamespace(publish=AsyncMock())
    sink = build_venue_hint_sink(
        scope=SCOPE, request_resync=requests.append, bus=bus, monotonic=lambda: 0.0,
    )
    await sink.offer_closed(OFFER)
    await sink.offer_closed(replace(OFFER, venue_seq=18, occurred_at_ms=999_999_999))
    await sink.offer_closed(replace(OFFER, venue_offer_id="43"))
    await sink.credit_closed(replace(CREDIT, credit_id=42))
    assert len(requests) == 3
    assert bus.publish.await_count == 4


@pytest.mark.asyncio
async def test_failed_resync_is_retried_before_notification():
    requests = []

    def request(reason):
        requests.append(reason)
        if len(requests) == 1:
            raise RuntimeError("resync callback failed")

    bus = SimpleNamespace(publish=AsyncMock())
    sink = build_venue_hint_sink(scope=SCOPE, request_resync=request, bus=bus, monotonic=lambda: 0)
    with pytest.raises(RuntimeError, match="resync callback failed"):
        await sink.offer_closed(OFFER)
    bus.publish.assert_not_awaited()
    await sink.offer_closed(OFFER)
    assert len(requests) == 2
    bus.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_hint_keys_are_pruned():
    now = [0.0]
    sink = build_venue_hint_sink(
        scope=SCOPE, request_resync=lambda _: None,
        bus=SimpleNamespace(publish=AsyncMock()), monotonic=lambda: now[0],
    )
    await sink.offer_closed(OFFER)
    now[0] = 5.0
    await sink.offer_closed(replace(OFFER, venue_offer_id="43"))
    assert sink._requested_at == {("offer_closed", "43"): 5.0}


@pytest.mark.asyncio
async def test_dispatcher_hands_ws_events_to_the_injected_sink():
    from bfx_funding_bot.external.bitfinex.auth_ws import FccEvent, FcnEvent, FocEvent

    sink = SimpleNamespace(offer_closed=AsyncMock(), credit_closed=AsyncMock())
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=SimpleNamespace(),
        event_sink=SimpleNamespace(), clock=lambda: 9000, venue_hint_sink=sink)
    dispatcher._recent_cancels["42"] = 8500
    await dispatcher._process(FocEvent(
        "42", "fUST", 1000, 8000, Decimal(100), "EXECUTED", 0.0005, 2, 17,
    ))
    sink.offer_closed.assert_awaited_once_with(replace(OFFER, cancel_requested_at_ms=8500))
    await dispatcher._process(FccEvent(
        81, "fUST", 1000, 2000, Decimal("100.00"), "CLOSED", 0.0005, 2, 17,
        mts_opening=1500, mts_last_payout=8000,
    ))
    sink.credit_closed.assert_awaited_once_with(replace(CREDIT, mts_opening=1500, mts_last_payout=8000))
    await dispatcher._process(FcnEvent(
        81, "fUST", 1000, 2000, Decimal(100), "ACTIVE", 0.0005, 2, 18,
    ))
    assert sink.offer_closed.await_count == sink.credit_closed.await_count == 1
