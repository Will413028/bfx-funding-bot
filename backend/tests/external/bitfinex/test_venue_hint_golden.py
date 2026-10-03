"""Captured on unchanged main 8617055d before the VenueHintSink refactor.

JSONB has no byte order: pin canonical serialize_event bytes, including every
field and bus-assigned metadata. Only the random event_id is normalized.
The PG companion checks the same bytes in actual event_log rows.
"""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.auth_ws import FccEvent, FocEvent
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.events import CreditClosed, OrderFilled, ReservationReleased
from bfx_funding_bot.modules.execution.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.legacy_venue_hints import LegacyVenueHintSink
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    OfferRegistry,
    RegistryState,
)
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.strategy import StrategyName

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CORRELATION = UUID("11111111-2222-4333-8444-555555555555")
CASES = {
    "executed": ("EXECUTED @ 0.0005 (100.0)", None),
    "expired": ("EXPIRED", None),
    "canceled": ("CANCELED", None),
    "cancelled": ("CANCELLED", None),
    "user_cancel_boundary": ("CANCELED", 4000),
    "cancel_outside_window": ("CANCELED", 3999),
    "unknown": ("CLOSED (used)", None),
    "executed_precedence": ("EXPIRED EXECUTED", None),
    "expired_precedence": ("CANCELED EXPIRED", 4000),
    "credit_closed": None,
    "credit_closed_fallback": None,
    "offer_gone": None,
}
GOLDEN = Path(__file__).with_name("venue_hint_golden_8617055d.json")


def payload_bytes(payload):
    payload = dict(payload)
    payload["event_id"] = "<generated>"
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class CapturePersister:
    def __init__(self):
        self.payloads = []
        self.sequences = set()
        self.timeline = []

    async def persist(self, *events):
        self.timeline.append("persist")
        statuses = []
        for event in events:
            key = (type(event), event.venue_seq)
            dedupable = isinstance(event, (OrderFilled, ReservationReleased))
            fresh = not dedupable or event.venue_seq is None or key not in self.sequences
            self.sequences.add(key)
            if fresh:
                self.payloads.append(payload_bytes(serialize_event(event)).decode())
            statuses.append(fresh)
        return statuses


def registry():
    result = OfferRegistry(clock=lambda: 9000)
    result._snapshot = {"42": ClaimRecord(
        venue_offer_id="42", cid=7, signal_correlation_id=CORRELATION,
        size_usdt=Decimal("100.00"), account_id=ACCOUNT, state=RegistryState.CLAIMED,
        occurred_at_ms=1000, last_updated_ms=1000, symbol="fUST",
        reservation_ref=ReservationRef(
            execution_decision_id="d-hint-7", cid=7,
            signal_correlation_id=CORRELATION, venue_offer_id="42",
        ),
    )}
    return result


async def scenario(case, monkeypatch, persister=None, *, repeat=False):
    persister = persister if persister is not None else CapturePersister()
    bus = DomainEventBus(clock=lambda: 9500)
    published = []

    async def capture(event):
        if isinstance(persister, CapturePersister):
            persister.timeline.append("publish")
        published.append(payload_bytes(serialize_event(event)).decode())

    for cls in (OrderFilled, ReservationReleased, CreditClosed):
        bus.subscribe(cls, capture)
    reg = registry()
    if case == "offer_gone":
        monkeypatch.setattr("bfx_funding_bot.modules.execution.fill_tracker.time.time", lambda: 9.0)
        tracker = RestPollingFillTracker(
            http=SimpleNamespace(), event_sink=SimpleNamespace(), probe=HealthProbe(), phase=Phase.LIVE, strategy=StrategyName.RATE_PERCENTILE,
            cell="C-1", account_id=ACCOUNT, venue_hint_sink=LegacyVenueHintSink(registry=reg, bus=bus, persister=persister, account_id=ACCOUNT))
        tracker._last_state = {"42": {"cid": 7, "status": "ACTIVE", "size": 100.0}}
        assert await tracker._diff_and_emit({}) == set()
    else:
        dispatcher = BitfinexLiveWSDispatcher(
            ws_client=SimpleNamespace(),
            event_sink=SimpleNamespace(), clock=lambda: 9000, venue_hint_sink=LegacyVenueHintSink(registry=reg, bus=bus, persister=persister, account_id=ACCOUNT))
        if case.startswith("credit_closed"):
            event = FccEvent(
                credit_id=81, symbol="fUST", mts_create=1000, mts_update=2000,
                amount=Decimal("100.00"), status="CLOSED", rate=0.0005,
                period_days=2, raw_seq=17, mts_opening=1500,
                mts_last_payout=8000 if case == "credit_closed" else None,
            )
        else:
            status, cancel_ms = CASES[case]
            if cancel_ms is not None:
                dispatcher._recent_cancels["42"] = cancel_ms
            event = FocEvent(
                venue_offer_id="42", symbol="fUST", mts_create=1000, mts_update=8000,
                amount=Decimal("99.00"), status=status, rate=0.0005,
                period_days=2, raw_seq=17,
            )
        await dispatcher._process(event)
        if repeat:
            # Keep registry CLAIMED to exercise durable venue_seq dedup,
            # rather than short-circuiting on an in-memory RELEASED claim.
            await dispatcher._process(event)
    return persister, published


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_legacy_hint_payload_and_bus_golden(case, monkeypatch):
    persister, published = await scenario(case, monkeypatch)
    expected = json.loads(GOLDEN.read_text())[case]
    assert persister.payloads == expected["persisted"]
    assert published == expected["published"]
    assert persister.timeline == ["persist", "publish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["executed", "canceled"])
async def test_legacy_venue_seq_dedup_suppresses_republish(case, monkeypatch):
    persister, published = await scenario(case, monkeypatch, repeat=True)
    expected = json.loads(GOLDEN.read_text())[case]
    assert persister.payloads == expected["persisted"]
    assert published == expected["published"]
    assert persister.timeline == ["persist", "publish", "persist"]


@pytest.mark.asyncio
async def test_legacy_credit_redelivery_does_not_use_offer_dedup(monkeypatch):
    persister, published = await scenario("credit_closed", monkeypatch, repeat=True)
    expected = json.loads(GOLDEN.read_text())["credit_closed"]
    assert persister.payloads == expected["persisted"] * 2
    second = json.loads(expected["published"][0])
    second["event_seq"] = 2
    assert published == [expected["published"][0], payload_bytes(second).decode()]
    assert persister.timeline == ["persist", "publish", "persist", "publish"]
