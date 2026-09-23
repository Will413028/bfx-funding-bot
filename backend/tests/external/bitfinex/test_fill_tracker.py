"""RestPollingFillTracker: atomic poll, venue_offer_id diff, paper-prefix invariant, schema validation."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jsonschema
import pytest

from bfx_funding_bot.external.bitfinex.fill_tracker import (
    CONSECUTIVE_FAIL_THRESHOLD,
    InvariantError,
    RestPollingFillTracker,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationReleased
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthTarget,
    Phase,
    StrategyName,
)

_SCHEMA = json.loads(
    (Path(__file__).parent.parent.parent / "contracts" / "bitfinex_funding_api_schema.json")
    .read_text()
)


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.degraded = asyncio.Event()

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if (
            event["event_type"] == EventType.HEALTH_CHECK.value
            and event["payload"].get("check_target") == HealthTarget.FILL_TRACKER.value
            and event["payload"].get("status") == "degraded"
        ):
            self.degraded.set()


def _validate_against_schema(resp: list, ref: str) -> None:
    validator = jsonschema.Draft202012Validator({"$ref": ref, **_SCHEMA})
    errors = list(validator.iter_errors(resp))
    assert not errors, errors


def _offer(venue_id: int, cid: int, status: str = "ACTIVE", remaining: float = 100.0) -> list:
    """Build a FundingOffer array. venue_id at index 0, cid at index 20."""
    return [
        venue_id, "fUSD", 1700000000000, 1700000000000,
        remaining, 100.0, "LIMIT",
        None, None, None, status,
        None, None, None,
        0.0001, 2, 0, 0, None, 0, cid,
    ]


def _credit(venue_id: int, amount: float = 100.0) -> list:
    """Build a FundingCredit array. NOTE: no cid field — credits lose the original offer's cid."""
    return [
        venue_id, "fUSD", "LEND", 1700000000000, 1700000000000,
        amount, None, "ACTIVE", 0,
        None, None,
        0.0001, 2, 1700000000000, 1700000000000,
        0, 0, None, 0, 0.0001, 0, "BTCUSD",
    ]


def _build_tracker(client: httpx.AsyncClient, axiom: _EventCapture,
                   bus: DomainEventBus | None = None,
                   registry: OfferRegistry | None = None) -> RestPollingFillTracker:
    probe = HealthProbe()
    if registry is None:
        registry = OfferRegistry(clock=lambda: 5000)
    return RestPollingFillTracker(
        http=client, event_sink=axiom, probe=probe, bus=bus or DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default", registry=registry, poll_interval_s=0.01,
    )


@pytest.mark.asyncio
async def test_atomic_poll_aborts_on_credits_failure() -> None:
    """Both /offers and /credits must succeed for a tick to count. If credits 500s,
    no events emit and last_state is preserved (next tick retries fresh)."""
    axiom = _EventCapture()

    def handler(req: httpx.Request) -> httpx.Response:
        if "offers" in req.url.path:
            return httpx.Response(200, json=[_offer(venue_id=111, cid=1)])
        return httpx.Response(500)  # credits fails

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bitfinex.com",
    ) as client:
        tracker = _build_tracker(client, axiom)
        # Seed last_state so disappearance would otherwise emit cancelled.
        tracker._last_state = {"222": {"cid": 2, "status": "ACTIVE"}}  # type: ignore[attr-defined]
        stop = asyncio.Event()

        task = asyncio.create_task(tracker.poll_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    order_events = [e for e in axiom.events
                    if e["event_type"] in (
                        EventType.ORDER_FILL.value,
                        EventType.ORDER_STATUS_CHANGE.value,
                    )]
    assert order_events == []
    # last_state preserved for next tick retry.
    assert tracker._last_state == {"222": {"cid": 2, "status": "ACTIVE"}}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_consecutive_failure_emits_degraded() -> None:
    """After CONSECUTIVE_FAIL_THRESHOLD consecutive tick failures, emit health_check degraded."""
    axiom = _EventCapture()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bitfinex.com",
    ) as client:
        tracker = _build_tracker(client, axiom)
        stop = asyncio.Event()

        task = asyncio.create_task(tracker.poll_loop(stop))
        try:
            await asyncio.wait_for(axiom.degraded.wait(), timeout=5.0)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=5.0)

    assert CONSECUTIVE_FAIL_THRESHOLD >= 1  # sanity import use
    hc = [e for e in axiom.events
          if e["event_type"] == EventType.HEALTH_CHECK.value
          and e["payload"].get("check_target") == HealthTarget.FILL_TRACKER.value]
    assert any(e["payload"]["status"] == "degraded" for e in hc)


@pytest.mark.asyncio
async def test_offer_disappearance_emits_reservation_released() -> None:
    """Core diff: offer present in last tick, absent this tick → emit ReservationReleased
    via bus. Tracker uses venue_offer_id as bridge.
    Registry seeded with claim for voi=111 to satisfy new registry-aware contract."""
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
        cid=42, venue_offer_id="111", size_usdt=Decimal("100.0"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
        symbol="fUST", reservation_ref=ReservationRef(
            execution_decision_id="d-fill-tracker", cid=42,
            signal_correlation_id=sig_id, venue_offer_id="111",
        ),
    ))

    tick_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal tick_count
        if "offers" in req.url.path:
            tick_count += 1
            if tick_count == 1:
                return httpx.Response(200, json=[_offer(venue_id=111, cid=42)])
            # Tick 2+: offer 111 has disappeared.
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])  # credits always empty

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bitfinex.com",
    ) as client:
        tracker = _build_tracker(client, axiom, bus=bus, registry=registry)
        stop = asyncio.Event()

        task = asyncio.create_task(tracker.poll_loop(stop))
        await asyncio.sleep(0.05)  # allow at least 2 ticks
        stop.set()
        await task

    assert len(released) >= 1
    ev = released[0]
    assert ev.cid == 42
    assert ev.venue_offer_id == "111"
    assert ev.reason == "missing_from_venue"
    assert ev.size_usdt == Decimal("100.0")  # from offer row[5] = AMOUNT_ORIG
    assert not ev.is_simulated


@pytest.mark.asyncio
async def test_paper_offer_id_invariant_at_emit_time() -> None:
    """CC4 defense-in-depth: if a paper_ prefixed offer_id leaks into tracker state
    (registry CC4 should prevent this at startup; this guards against bypass),
    emit path raises InvariantError → propagates to TaskGroup → daemon restart."""
    axiom = _EventCapture()

    def handler(req: httpx.Request) -> httpx.Response:
        if "offers" in req.url.path:
            return httpx.Response(200, json=[])  # paper_abc has disappeared
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bitfinex.com",
    ) as client:
        tracker = _build_tracker(client, axiom)
        # Seed with a paper_ offer_id — simulates wiring bug where paper executor's
        # state leaked into a tracker that thinks it's polling real venue.
        tracker._last_state = {"paper_abc": {"cid": 1, "status": "ACTIVE"}}  # type: ignore[attr-defined]
        stop = asyncio.Event()

        task = asyncio.create_task(tracker.poll_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        with pytest.raises(InvariantError):
            await task


def test_schema_offer_sample_validates() -> None:
    _validate_against_schema(
        [_offer(venue_id=111, cid=42)], "#/definitions/FundingOffersResponse",
    )


def test_schema_credit_sample_validates() -> None:
    _validate_against_schema([_credit(venue_id=222)], "#/definitions/FundingCreditsResponse")
