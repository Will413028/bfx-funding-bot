"""RestPollingFillTracker: atomic poll, venue_offer_id diff, paper-prefix invariant, schema validation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from bfx_funding_bot.modules.execution.fill_tracker import (
    CONSECUTIVE_FAIL_THRESHOLD,
    InvariantError,
    RestPollingFillTracker,
)
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


class _CaptureAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


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


def _make_tracker(axiom: _CaptureAxiom, *, transport: httpx.MockTransport) -> RestPollingFillTracker:
    probe = HealthProbe()
    client = httpx.AsyncClient(transport=transport, base_url="https://api.bitfinex.com")
    return RestPollingFillTracker(
        http=client, axiom=axiom, probe=probe,
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default", poll_interval_s=0.01,
    )


@pytest.mark.asyncio
async def test_atomic_poll_aborts_on_credits_failure() -> None:
    """Both /offers and /credits must succeed for a tick to count. If credits 500s,
    no events emit and last_state is preserved (next tick retries fresh)."""
    axiom = _CaptureAxiom()

    def handler(req: httpx.Request) -> httpx.Response:
        if "offers" in req.url.path:
            return httpx.Response(200, json=[_offer(venue_id=111, cid=1)])
        return httpx.Response(500)  # credits fails

    tracker = _make_tracker(axiom, transport=httpx.MockTransport(handler))
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
    axiom = _CaptureAxiom()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    tracker = _make_tracker(axiom, transport=httpx.MockTransport(handler))
    stop = asyncio.Event()

    task = asyncio.create_task(tracker.poll_loop(stop))
    # Enough ticks to exceed CONSECUTIVE_FAIL_THRESHOLD (poll_interval_s=0.01).
    await asyncio.sleep(0.1)
    stop.set()
    await task

    assert CONSECUTIVE_FAIL_THRESHOLD >= 1  # sanity import use
    hc = [e for e in axiom.events
          if e["event_type"] == EventType.HEALTH_CHECK.value
          and e["payload"].get("check_target") == HealthTarget.FILL_TRACKER.value]
    assert any(e["payload"]["status"] == "degraded" for e in hc)


@pytest.mark.asyncio
async def test_offer_disappearance_emits_status_change() -> None:
    """Core diff: offer present in last tick, absent this tick → emit order_status_change
    status=filled_or_cancelled, reason=missing_from_venue. Tracker uses venue_offer_id as bridge."""
    axiom = _CaptureAxiom()
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

    tracker = _make_tracker(axiom, transport=httpx.MockTransport(handler))
    stop = asyncio.Event()

    task = asyncio.create_task(tracker.poll_loop(stop))
    await asyncio.sleep(0.05)  # allow at least 2 ticks
    stop.set()
    await task

    status_changes = [e for e in axiom.events
                      if e["event_type"] == EventType.ORDER_STATUS_CHANGE.value]
    assert len(status_changes) >= 1
    payload = status_changes[0]["payload"]
    assert payload["offer_id"] == "111"
    assert payload["cid"] == 42
    assert payload["status"] == "filled_or_cancelled"
    assert payload["reason"] == "missing_from_venue"


@pytest.mark.asyncio
async def test_paper_offer_id_invariant_at_emit_time() -> None:
    """CC4 defense-in-depth: if a paper_ prefixed offer_id leaks into tracker state
    (registry CC4 should prevent this at startup; this guards against bypass),
    emit path raises InvariantError → propagates to TaskGroup → daemon restart."""
    axiom = _CaptureAxiom()

    def handler(req: httpx.Request) -> httpx.Response:
        if "offers" in req.url.path:
            return httpx.Response(200, json=[])  # paper_abc has disappeared
        return httpx.Response(200, json=[])

    tracker = _make_tracker(axiom, transport=httpx.MockTransport(handler))
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
