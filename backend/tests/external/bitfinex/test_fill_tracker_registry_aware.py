from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry
from bfx_funding_bot.modules.strategy import StrategyName


def _ref(cid: int, scid, voi: str = "42") -> ReservationRef:
    return ReservationRef(
        execution_decision_id=f"d-fill-tracker-{cid}", cid=cid,
        signal_correlation_id=scid, venue_offer_id=voi,
    )


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.asyncio
async def test_fill_tracker_skips_emit_when_registry_already_released() -> None:
    """G1: registry already RELEASED → fill_tracker dedup."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)
    bus.subscribe(ReservationClaimed, registry.handle)
    bus.subscribe(ReservationReleased, registry.handle)

    sig_id = uuid4()
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    symbol="fUST", reservation_ref=_ref(42, sig_id)))
    await bus.publish(ReservationReleased(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        reason="user_cancel", signal_correlation_id=sig_id,
        account_id="default", is_simulated=False, occurred_at_ms=2000,
    symbol="fUST", reservation_ref=_ref(42, sig_id)))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.bitfinex.com",
    )

    captured: list = []
    async def capture(ev: ReservationReleased) -> None:
        captured.append(ev)
    bus.subscribe(ReservationReleased, capture)
    pre_count = len(captured)

    tracker = RestPollingFillTracker(
        http=http, event_sink=_EventCapture(), probe=HealthProbe(),
        bus=bus, phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        cell="C-1", account_id="default", registry=registry,
    )
    tracker._last_state = {
        "42": {"cid": 42, "status": "ACTIVE", "size": 100.0},
    }
    await tracker._tick()

    # Registry already RELEASED → tracker should skip emit
    assert len(captured) == pre_count


@pytest.mark.asyncio
async def test_fill_tracker_emits_with_claim_correlation_id_not_uuid4() -> None:
    """G3 fix: signal_correlation_id from registry lookup, not uuid4()."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)
    bus.subscribe(ReservationClaimed, registry.handle)

    sig_id = uuid4()
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    symbol="fUST", reservation_ref=_ref(42, sig_id)))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.bitfinex.com",
    )
    captured: list = []
    async def capture(ev: ReservationReleased) -> None:
        captured.append(ev)
    bus.subscribe(ReservationReleased, capture)

    tracker = RestPollingFillTracker(
        http=http, event_sink=_EventCapture(), probe=HealthProbe(),
        bus=bus, phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        cell="C-1", account_id="default", registry=registry,
    )
    tracker._last_state = {"42": {"cid": 42, "status": "ACTIVE", "size": 100.0}}
    await tracker._tick()

    assert len(captured) == 1
    # Critical: correlation_id from registry, NOT uuid4
    assert captured[0].signal_correlation_id == sig_id
    assert captured[0].reason == "missing_from_venue"


@pytest.mark.asyncio
async def test_fill_tracker_skips_when_voi_not_in_registry() -> None:
    """voi disappeared but never had claim in registry (boot-before-claim) → skip emit + log."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.bitfinex.com",
    )
    captured: list = []
    async def capture(ev: ReservationReleased) -> None:
        captured.append(ev)
    bus.subscribe(ReservationReleased, capture)

    tracker = RestPollingFillTracker(
        http=http, event_sink=_EventCapture(), probe=HealthProbe(),
        bus=bus, phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        cell="C-1", account_id="default", registry=registry,
    )
    tracker._last_state = {"99": {"cid": 99, "status": "ACTIVE", "size": 50.0}}
    await tracker._tick()

    assert len(captured) == 0  # no event for unknown voi


@pytest.mark.asyncio
async def test_fill_tracker_reservation_released_carries_claim_symbol() -> None:
    """Symbol from ReservationClaimed threads through to emitted ReservationReleased."""
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)
    bus.subscribe(ReservationClaimed, registry.handle)

    sig_id = uuid4()
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000, symbol="fUST", reservation_ref=_ref(42, sig_id),
    ))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.bitfinex.com",
    )
    captured: list = []
    async def capture(ev: ReservationReleased) -> None:
        captured.append(ev)
    bus.subscribe(ReservationReleased, capture)

    tracker = RestPollingFillTracker(
        http=http, event_sink=_EventCapture(), probe=HealthProbe(),
        bus=bus, phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        cell="C-1", account_id="default", registry=registry,
    )
    tracker._last_state = {"42": {"cid": 42, "status": "ACTIVE", "size": 100.0}}
    await tracker._tick()

    assert len(captured) == 1
    # symbol must be threaded from claim, not fall back to default "fUSD"
    assert captured[0].symbol == "fUST"
