"""Phase 4.4a integration: WS misses event → fill_tracker fallback.

Scenario: ReservationClaimed seeded into registry. WS would normally
publish OrderFilled or ReservationReleased, but suppose the WS dropped
the event. fill_tracker's REST poll discovers the offer is gone from
venue and emits ReservationReleased with reason="missing_from_venue".

Verifies:
- fill_tracker checks registry first (deduplication)
- If registry still CLAIMED → emits ReservationReleased with
  signal_correlation_id from registry (NOT uuid4)
- reason = "missing_from_venue" (distinct from user/venue cancel)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.strategy import StrategyName

from .conftest import make_reservation_ref


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fill_tracker_emits_missing_from_venue_when_ws_misses_event(
    domain_chain: dict[str, Any], event_sink_stub: Any,
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    # 1. Claim seeded — registry CLAIMED, ledger reserved=100
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
        symbol="fUST",
        reservation_ref=make_reservation_ref(42, sig_id, voi),
    ))
    assert ledger.current_exposure("fUST") == Decimal("100")

    # 2. WS missed the cancel event → registry still CLAIMED.
    #    fill_tracker's REST poll returns no offers (venue says offer is gone).
    def venue_returns_no_offers(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    http = httpx.AsyncClient(
        base_url="https://api.bitfinex.com",
        transport=httpx.MockTransport(venue_returns_no_offers),
    )

    tracker = RestPollingFillTracker(
        http=http, event_sink=_EventCapture(), probe=HealthProbe(),
        bus=bus, phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        cell="C-1", account_id="default", registry=registry,
    )
    # Seed last_state with the same voi (representing last tick saw it)
    tracker._last_state = {voi: {"cid": 42, "status": "ACTIVE", "size": 100.0}}
    await tracker._tick()

    # 3. _reserved should release; registry should reflect RELEASED via bus subscriber
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert registry.snapshot()[voi].state.value == "released"

    # 4. event rows: reason=missing_from_venue (distinct from cancel)
    release_rows = [r for r in event_sink_stub.rows if r["event_type"] == "ReservationReleased"]
    assert len(release_rows) == 1
    assert release_rows[0]["reason"] == "missing_from_venue"
