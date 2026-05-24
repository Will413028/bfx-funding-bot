"""Phase 4.4a integration: submit → cancel → foc CANCELED → user_cancel reason.

Tests the CancelRequested event flow:
1. User submits offer (registry CLAIMED)
2. User triggers cancel → CancelRequested published directly to bus
3. Dispatcher subscribed to CancelRequested tracks recent_cancels
4. WS foc CANCELED arrives within 5s → reason=user_cancel
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import FocEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    ReservationClaimed,
)

from .conftest import ScriptedWSClient


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_then_foc_emits_user_cancel(
    domain_chain: dict[str, Any], event_sink_stub: Any,
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    # 1. Seed claim — registry CLAIMED, ledger reserved=100
    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    ))

    # 2. Build dispatcher and subscribe to CancelRequested
    #    clock=2100 → foc CANCELED at mts_update=2000, requested_at_ms=2000
    #    δ = 2100 - 2000 = 100 ms < 5000 ms window → user_cancel
    foc = FocEvent(
        venue_offer_id=voi, symbol="fUSD",
        mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="CANCELED",
        rate=0.0005, period_days=2, raw_seq=7, raw=[],
    )
    fake_ws = ScriptedWSClient([foc])
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 2100, queue_max=100,
    )
    bus.subscribe(CancelRequested, dispatcher.handle_cancel_requested)

    # 3. Directly publish CancelRequested with deterministic timestamp
    #    (avoids relying on real time.time() in executor.cancel)
    await bus.publish(CancelRequested(
        venue_offer_id=voi, requested_at_ms=2000,
        signal_correlation_id=sig_id, account_id="default",
    ))

    # 4. Run dispatcher; foc arrives → dispatcher checks recent_cancels (just added)
    #    foc CANCELED with recent_cancels has voi=42 mts ~current → user_cancel
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # 5. Assertions
    assert ledger.current_exposure() == Decimal("0")
    assert registry.snapshot()[voi].state.value == "released"

    release_rows = [r for r in event_sink_stub.rows if r["event_type"] == "ReservationReleased"]
    assert len(release_rows) == 1
    assert release_rows[0]["reason"] == "user_cancel"

    cancel_rows = [r for r in event_sink_stub.rows if r["event_type"] == "CancelRequested"]
    assert len(cancel_rows) == 1  # cancel auditable in event log
