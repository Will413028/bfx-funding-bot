"""ReservationEmittingMiddleware — emit ReservationClaimed + OrderFilled.

Middle layer in chain: after retry resolves to final result, before
heartbeat outer wrap. Publishes domain events to DomainEventBus; ledger
+ axiom_sink subscribe.

I3-EM: status="failed" → no emit (防 ledger 漏洞).
I4-EM: bus.publish raise → swallow + log (venue submit 已成，不回滾).
I8-Order: same submit 內 CLAIMED publish 先於 FILLED (paper sync only).

FORWARD-COMPAT (Phase 5+): bus.publish call site is the outbox/DLQ upgrade
hook. Future will replace gather() fanout with outbox-write + reconciler.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

log = logging.getLogger(__name__)


class ReservationEmittingMiddleware:
    """ExecutorPort wrapper that emits domain events after successful submit."""

    def __init__(self, inner: ExecutorPort, *, bus: DomainEventBus) -> None:
        self._inner = inner
        self._bus = bus

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        result = await self._inner.submit(decision, ctx)
        if result.status in ("submitted", "filled"):
            await self._safe_publish(ReservationClaimed(
                cid=result.cid,
                venue_offer_id=result.venue_offer_id or "",
                size_usdt=Decimal(str(decision.offer_amount_usdt or 0.0)),
                signal_correlation_id=decision.signal_correlation_id,
                account_id=ctx.account_id,
                is_simulated=(result.raw_response is None),
            ))
        if result.status == "filled":
            await self._safe_publish(OrderFilled(
                cid=result.cid,
                venue_offer_id=result.venue_offer_id or "",
                credit_id=None,  # 4.4 BitfinexLive WS fcn handler populates
                size_usdt=Decimal(str(decision.offer_amount_usdt or 0.0)),
                fill_rate=decision.offer_rate or 0.0,
                signal_correlation_id=decision.signal_correlation_id,
                account_id=ctx.account_id,
                is_simulated=(result.raw_response is None),
            ))
        return result

    async def _safe_publish(self, event: object) -> None:
        """I4-EM: bus failure 不該讓 submit 失敗.

        Bus internally already isolates handler exceptions (gather +
        return_exceptions=True); this outer try/except guards against
        defects in bus.publish itself (extremely rare).
        """
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical(
                "bus_publish_failed_outer event=%s err=%r — telemetry lost, "
                "venue submit already succeeded",
                type(event).__name__, exc,
            )
