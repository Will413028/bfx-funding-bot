"""G1: At-least-once + Idempotent = Exactly-once-effect.

Per spec §4 G1. Duplicate WS event delivery → projection state +1 once.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import FocEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.events import ReservationClaimed

from .conftest import ScriptedWSClient, make_reservation_ref


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_foc_executed_does_not_double_realize(
    domain_chain: dict[str, Any], event_sink_stub: Any,
) -> None:
    """G1 invariant: same foc EXECUTED delivered twice → _realized +100 ONCE."""
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
        symbol="fUSD",
        reservation_ref=make_reservation_ref(42, sig_id, voi),
    ))

    # Same foc EXECUTED delivered twice — venue WS may redeliver
    foc1 = FocEvent(
        venue_offer_id=voi, symbol="fUSD",
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), status="EXECUTED @ 0.0005 (100.0)",
        rate=0.0005, period_days=2, raw_seq=5, raw=[],
    )
    foc2 = FocEvent(
        venue_offer_id=voi, symbol="fUSD",
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), status="EXECUTED @ 0.0005 (100.0)",
        rate=0.0005, period_days=2, raw_seq=5, raw=[],  # same venue_seq
    )
    fake_ws = ScriptedWSClient([foc1, foc2])
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 2500, queue_max=100,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.4)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # G1 invariant: realized=100 NOT 200; registry RELEASED idempotent
    assert ledger.realized_exposure("fUSD") == Decimal("100")
    assert registry.snapshot()[voi].state.value == "released"
