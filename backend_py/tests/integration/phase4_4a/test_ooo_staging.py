"""Phase 4.4a integration: fcn arrives before ReservationClaimed (OOO race).

Verifies: dispatcher OOO staging buffer (200ms TTL) holds the fcn,
then drains when claim lands → OrderFilled emitted.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import FcnEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed

from .conftest import ScriptedWSClient


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fcn_arrives_before_claim_then_drained_after_claim(
    domain_chain: dict[str, Any], axiom_sink: Any,
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    # fcn with offer_id_meta=42 → dispatcher maps to "42" lookup
    fcn = FcnEvent(
        credit_id=999, symbol="fUSD", side=1,
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2,
        offer_id_meta=42, raw_seq=5, raw=[],
    )

    fake_ws = ScriptedWSClient([fcn])

    # controllable clock — starts at 500ms
    clock_ms = [500]
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: clock_ms[0], queue_max=100,
    )

    captured: list[OrderFilled] = []
    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)
    bus.subscribe(OrderFilled, capture)

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))

    # Step 1: fcn arrives at clock=500 — staged (no claim in registry yet)
    await asyncio.sleep(0.1)
    assert len(captured) == 0  # not yet emitted

    # Step 2: ReservationClaimed at clock=600 (within 200ms TTL of staging)
    clock_ms[0] = 600
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=550,
    ))

    # Step 3: Wait for dispatcher's maintenance tick to drain staging
    # Dispatcher's run loop has a 0.5s timeout on queue.get, then maintenance.
    # Push clock forward and wait long enough for at least one maintenance tick.
    await asyncio.sleep(0.7)

    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # OrderFilled emitted after claim landed
    assert len(captured) == 1
    assert captured[0].credit_id == "999"
    assert ledger.realized_exposure() == Decimal("100")
