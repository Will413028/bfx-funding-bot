"""AxiomEventSink — bus subscriber that maps domain events to Axiom emit payload."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.marketfeed.schemas import EventType, Phase, StrategyName


class _CapturingAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _claim() -> ReservationClaimed:
    return ReservationClaimed(
        cid=42, venue_offer_id="paper_abc", size_usdt=Decimal("100.5"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )


def _fill() -> OrderFilled:
    return OrderFilled(
        cid=42, venue_offer_id="paper_abc", credit_id=None,
        size_usdt=Decimal("100.5"), fill_rate=0.0002,
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )


def _release() -> ReservationReleased:
    return ReservationReleased(
        cid=42, venue_offer_id="paper_abc", size_usdt=Decimal("100.5"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=True,
    )


@pytest.mark.asyncio
async def test_reservation_claimed_emits_with_correct_event_type() -> None:
    axiom = _CapturingAxiom()
    sink = AxiomEventSink(axiom_client=axiom, phase=Phase.PAPER,
                          strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT")
    await sink.on_reservation_claimed(_claim())
    assert len(axiom.events) == 1
    ev = axiom.events[0]
    assert ev["event_type"] == EventType.RESERVATION_CLAIMED.value
    assert ev["payload"]["cid"] == 42
    assert ev["payload"]["venue_offer_id"] == "paper_abc"
    assert ev["payload"]["size_usdt"] == 100.5


@pytest.mark.asyncio
async def test_order_filled_emits_order_fill_event_type() -> None:
    axiom = _CapturingAxiom()
    sink = AxiomEventSink(axiom_client=axiom, phase=Phase.PAPER,
                          strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT")
    await sink.on_order_filled(_fill())
    assert axiom.events[0]["event_type"] == EventType.ORDER_FILL.value
    # OrderFillPayload schema uses offer_id/fill_size_usdt/fill_price field names
    assert axiom.events[0]["payload"]["offer_id"] == "paper_abc"
    assert axiom.events[0]["payload"]["fill_size_usdt"] == 100.5


@pytest.mark.asyncio
async def test_reservation_released_includes_reason() -> None:
    axiom = _CapturingAxiom()
    sink = AxiomEventSink(axiom_client=axiom, phase=Phase.PAPER,
                          strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT")
    await sink.on_reservation_released(_release())
    assert axiom.events[0]["event_type"] == EventType.RESERVATION_RELEASED.value
    assert axiom.events[0]["payload"]["reason"] == "venue_cancel"


@pytest.mark.asyncio
async def test_axiom_emit_failure_propagates() -> None:
    """Sink 不 swallow — let bus.gather 隔離."""
    class _Broken:
        async def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("axiom client final give-up")
    sink = AxiomEventSink(axiom_client=_Broken(), phase=Phase.PAPER,
                          strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT")
    with pytest.raises(RuntimeError):
        await sink.on_reservation_claimed(_claim())
