"""Path D — fill_tracker: 5 ticks with state transitions, assert diff events."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationReleased
from bfx_funding_bot.modules.execution.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.legacy_venue_hints import LegacyVenueHintSink
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry
from bfx_funding_bot.modules.strategy import StrategyName

pytestmark = pytest.mark.integration


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


def _offer(venue_id: int, cid: int) -> list[Any]:
    return [
        venue_id, "fUSD", 1700000000000, 1700000000000,
        100.0, 100.0, "LIMIT",
        None, None, None, "ACTIVE",
        None, None, None,
        0.0001, 2, 0, 0, None, 0, cid,
    ]


@pytest.mark.asyncio
async def test_fill_tracker_ticks_emit_status_changes() -> None:
    """Tick 1: offers=[venue 111 cid 1].
    Tick 2+: offers=[] — venue 111 disappeared → ReservationReleased via bus.
    Registry seeded with claim for voi=111 to satisfy new registry-aware contract.
    """
    tick_seen = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal tick_seen
        if "offers" in req.url.path:
            tick_seen += 1
            if tick_seen == 1:
                return httpx.Response(200, json=[_offer(venue_id=111, cid=1)])
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    axiom = _EventCapture()
    bus = DomainEventBus()
    released: list[ReservationReleased] = []

    async def capture(e: ReservationReleased) -> None:
        released.append(e)

    bus.subscribe(ReservationReleased, capture)

    # Seed registry with claim for voi="111" (option a: seed to satisfy new contract)
    registry = OfferRegistry(clock=lambda: 5000)
    sig_id = uuid4()
    bus.subscribe(ReservationClaimed, registry.handle)
    await bus.publish(ReservationClaimed(
        cid=1, venue_offer_id="111", size_usdt=Decimal("100.0"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    symbol="fUST", reservation_ref=ReservationRef(
        execution_decision_id="d-integration-fill-tracker", cid=1,
        signal_correlation_id=sig_id, venue_offer_id="111",
    )))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.bitfinex.com",
    ) as client:
        probe = HealthProbe()
        tracker = RestPollingFillTracker(
            http=client,
            event_sink=axiom,
            probe=probe,
            phase=Phase.SHADOW,
            strategy=StrategyName.MEAN_REVERSION,
            cell="fUSD_a30",
            account_id="default",
            poll_interval_s=0.01, venue_hint_sink=LegacyVenueHintSink(registry=registry, bus=bus, persister=NoopEventPersister(), account_id="default"))
        stop = asyncio.Event()
        task = asyncio.create_task(tracker.poll_loop(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    assert len(released) >= 1
    reasons = [e.reason for e in released]
    assert "missing_from_venue" in reasons
