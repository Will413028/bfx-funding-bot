"""Phase 4.4a integration: fcn is informational — no domain effect regardless of order.

fcn no longer drives the lifecycle. foc EXECUTED is the fill signal.
This test verifies fcn arriving before or after claim produces no OrderFilled.
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
async def test_fcn_before_or_after_claim_produces_no_orderfilled(
    domain_chain: dict[str, Any], event_sink_stub: Any,
) -> None:
    """fcn is informational — no offer id, no staging, no domain events."""
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    fcn = FcnEvent(
        credit_id=999, symbol="fUSD", side=1,
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2,
        raw_seq=5, raw=[],
    )

    fake_ws = ScriptedWSClient([fcn])

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 500, queue_max=100,
    )

    captured: list[OrderFilled] = []
    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)
    bus.subscribe(OrderFilled, capture)

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))

    # fcn arrives — still a no-op even after claim
    await asyncio.sleep(0.1)
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=550,
    symbol="fUSD"))
    await asyncio.sleep(0.7)

    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # fcn produces no OrderFilled; ledger unrealized (foc fill not sent)
    assert len(captured) == 0
    assert ledger.realized_exposure("fUSD") == Decimal("0")
