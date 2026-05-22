"""Phase 4.4a integration test shared fixtures.

Pattern: real DomainEventBus + real PaperPositionLedger + real OfferRegistry +
axiom_sink stub. Each test composes the chain with subscribers wired.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import BfxWSEvent
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry


class StubAxiomQuery:
    """No-op axiom query — 4.4a stub returns empty list."""
    async def fetch_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class StubAxiomSink:
    """Captures every domain event as a row for assertion."""
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.rows.append(event)

    async def handle(self, event: Any) -> None:
        """Bus subscriber — captures domain event as a row."""
        self.rows.append({
            "event_type": type(event).__name__,
            "venue_offer_id": getattr(event, "venue_offer_id", None),
            "event_seq": getattr(event, "event_seq", None),
            "occurred_at_ms": getattr(event, "occurred_at_ms", None),
            "credit_id": getattr(event, "credit_id", None),
            "reason": getattr(event, "reason", None),
        })


class ScriptedWSClient:
    """Drop-in for BitfinexAuthWSClient — emits pre-scripted events then holds open."""
    def __init__(self, events: list[BfxWSEvent]) -> None:
        self._events = list(events)
        self._stop = False

    async def events(self) -> AsyncIterator[BfxWSEvent]:
        for ev in self._events:
            if self._stop:
                return
            yield ev
            await asyncio.sleep(0.001)
        while not self._stop:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._stop = True


@pytest.fixture
def axiom_sink() -> StubAxiomSink:
    return StubAxiomSink()


@pytest.fixture
def domain_chain(axiom_sink: StubAxiomSink) -> dict[str, Any]:
    """Build (bus, ledger, registry) with subscribers wired.

    Returns: {"bus", "ledger", "registry", "axiom_sink"}
    """
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(axiom_query=StubAxiomQuery(), clock=lambda: 5000)
    ledger = PaperPositionLedger(account_id="default")

    bus.subscribe(ReservationClaimed, registry.handle)
    bus.subscribe(OrderFilled, registry.handle)
    bus.subscribe(ReservationReleased, registry.handle)
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    bus.subscribe(ReservationClaimed, axiom_sink.handle)
    bus.subscribe(OrderFilled, axiom_sink.handle)
    bus.subscribe(ReservationReleased, axiom_sink.handle)
    bus.subscribe(CancelRequested, axiom_sink.handle)

    return {"bus": bus, "registry": registry, "ledger": ledger, "axiom_sink": axiom_sink}
