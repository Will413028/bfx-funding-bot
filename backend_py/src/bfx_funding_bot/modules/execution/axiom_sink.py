"""AxiomEventSink — DomainEventBus subscriber that emits to Axiom.

Maps in-process domain events (events.py) to Axiom event-store payloads
(schemas.py Pydantic models). One sink subscribes to all three event types.

Axiom client (`external/axiom.py`) handles transient reliability via
internal buffer + retry. If client.emit() raises here (final give-up),
the exception propagates — DomainEventBus.publish() will isolate via
gather(return_exceptions=True) so other subscribers (ledger) keep working.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    Level,
    Phase,
    StrategyName,
)


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class AxiomEventSink:
    def __init__(
        self,
        *,
        axiom_client: _AxiomProtocol,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
    ) -> None:
        self._axiom = axiom_client
        self._phase = phase
        self._strategy = strategy
        self._cell = cell

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        await self._axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self._phase.value,
            "strategy": self._strategy.value,
            "cell": self._cell,
            "event_type": EventType.RESERVATION_CLAIMED.value,
            "correlation_id": str(event.signal_correlation_id),
            "account_id": event.account_id,
            "payload": {
                "cid": event.cid,
                "venue_offer_id": event.venue_offer_id,
                "size_usdt": float(event.size_usdt),
                "signal_correlation_id": str(event.signal_correlation_id),
                "is_simulated": event.is_simulated,
            },
        })

    async def on_order_filled(self, event: OrderFilled) -> None:
        await self._axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self._phase.value,
            "strategy": self._strategy.value,
            "cell": self._cell,
            "event_type": EventType.ORDER_FILL.value,
            "correlation_id": str(event.signal_correlation_id),
            "account_id": event.account_id,
            "payload": {
                # OrderFillPayload field names (existing schema)
                "cid": event.cid,
                "offer_id": event.venue_offer_id,
                "signal_correlation_id": str(event.signal_correlation_id),
                "fill_size_usdt": float(event.size_usdt),
                "fill_price": event.fill_rate,
                "is_simulated": event.is_simulated,
            },
        })

    async def handle_cancel_requested(self, event: CancelRequested) -> None:
        await self._axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self._phase.value,
            "strategy": self._strategy.value,
            "cell": self._cell,
            "event_type": EventType.CANCEL_REQUESTED.value,
            "correlation_id": str(event.signal_correlation_id),
            "account_id": event.account_id,
            "payload": {
                "venue_offer_id": event.venue_offer_id,
                "requested_at_ms": event.requested_at_ms,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
        })

    async def handle_cancel_acknowledged(self, event: CancelAcknowledged) -> None:
        await self._axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self._phase.value,
            "strategy": self._strategy.value,
            "cell": self._cell,
            "event_type": EventType.CANCEL_ACKNOWLEDGED.value,
            "correlation_id": str(event.signal_correlation_id),
            "account_id": event.account_id,
            "payload": {
                "venue_offer_id": event.venue_offer_id,
                "acknowledged_at_ms": event.acknowledged_at_ms,
                "rest_status": event.rest_status,
                "venue_response_text": event.venue_response_text,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
        })

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        await self._axiom.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self._phase.value,
            "strategy": self._strategy.value,
            "cell": self._cell,
            "event_type": EventType.RESERVATION_RELEASED.value,
            "correlation_id": str(event.signal_correlation_id),
            "account_id": event.account_id,
            "payload": {
                "cid": event.cid,
                "venue_offer_id": event.venue_offer_id,
                "size_usdt": float(event.size_usdt),
                "reason": event.reason,
                "signal_correlation_id": str(event.signal_correlation_id),
                "is_simulated": event.is_simulated,
            },
        })
