"""ReservationEmittingMiddleware — A2 write-ahead intent + sync SoT persistence.

Flow per submit:
  1. compute cid once (capture-once submit_date — INTENT/outcome share it)
  2. txn1: persist ReservationIntent (PENDING) — durable BEFORE venue submit
  3. inner.submit(cid) — the only non-transactional boundary (Bitfinex REST)
  4. txn2: persist outcome (CLAIMED[+ORDER_FILL] | RESERVATION_FAILED)
  5. bus.publish CLAIMED[+FILL] for in-memory projections (ledger/registry) +
     diagnostics. INTENT/FAILED are NOT published (no in-memory subscriber).

Each persist() is its own txn (EventStorePersister) so a txn is never held
across the REST call. A crash between txn1 and txn2 leaves a durable PENDING
claim, resolved at boot (3a-recovery).

I3-EM: status="failed" -> persist FAILED, no bus publish.
I4-EM: bus.publish raise -> swallow + log (persistence already committed).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)

log = logging.getLogger(__name__)


class ReservationEmittingMiddleware:
    """ExecutorPort wrapper: A2 write-ahead intent + sync persistence + bus fanout."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        bus: DomainEventBus,
        persister: EventPersister,
        is_simulated: bool = True,
        clock: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._inner = inner
        self._bus = bus
        self._persister = persister
        self._is_simulated = is_simulated
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._date_provider = date_provider or (lambda: datetime.now(UTC).date())

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        # This middleware is the cid authority (A2). Ignore any incoming cid;
        # compute once so INTENT and outcome share the exact same value.
        decision = ready.decision
        cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        reference = reservation_ref or ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=cid,
            signal_correlation_id=decision.signal_correlation_id,
        )
        if (
            reference.execution_decision_id != ready.decision_id
            or reference.cid != cid
            or reference.signal_correlation_id != decision.signal_correlation_id
        ):
            raise ValueError("reservation_ref conflicts with ReadyToSubmit request")
        size = Decimal(str(decision.offer_amount_usdt or 0.0))
        scid = decision.signal_correlation_id
        intent_ms = self._clock()

        # txn1: write-ahead intent (durable before the venue submit)
        await self._persister.persist(ReservationIntent(
            cid=cid, size_usdt=size, signal_correlation_id=scid,
            account_id=ctx.account_id, is_simulated=self._is_simulated,
            occurred_at_ms=intent_ms, symbol=decision.symbol,
            execution_decision_id=ready.decision_id,
            reservation_ref=reference,
        ))

        result = await self._inner.submit(ready, ctx, cid=cid, reservation_ref=reference)
        outcome_ms = self._clock()

        if result.reservation_ref is not None and result.reservation_ref != reference:
            raise RuntimeError("executor returned a conflicting reservation reference")
        bound_reference = reference
        if result.venue_offer_id is not None:
            bound_reference = reference.bind_venue_offer(result.venue_offer_id)
        result = replace(result, reservation_ref=bound_reference)

        if result.status in ("submitted", "filled"):
            claimed = ReservationClaimed(
                cid=cid, venue_offer_id=result.venue_offer_id or "",
                size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                occurred_at_ms=outcome_ms, symbol=decision.symbol,
                reservation_ref=bound_reference,
            )
            filled: OrderFilled | None = None
            if result.status == "filled":
                filled = OrderFilled(
                    cid=cid, venue_offer_id=result.venue_offer_id or "", credit_id=None,
                    size_usdt=size, fill_rate=decision.offer_rate or 0.0,
                    signal_correlation_id=scid, account_id=ctx.account_id,
                    is_simulated=self._is_simulated, occurred_at_ms=outcome_ms,
                    symbol=decision.symbol, reservation_ref=bound_reference,
                )
            # txn2: outcome (event_log + snapshot, atomic)
            if filled is not None:
                await self._persister.persist(claimed, filled)
            else:
                await self._persister.persist(claimed)
            # in-memory projections + diagnostics (after durable commit)
            await self._safe_publish(claimed)
            if filled is not None:
                await self._safe_publish(filled)
        elif result.status == "failed":
            # txn2: FAILED — reserved untouched
            await self._persister.persist(ReservationFailed(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason="submit_failed", occurred_at_ms=outcome_ms,
                symbol=decision.symbol, reservation_ref=bound_reference,
            ))
        return result

    async def _safe_publish(self, event: object) -> None:
        """I4-EM: bus failure must not fail submit. Persistence is already committed;
        bus is best-effort in-memory fanout."""
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical(
                "bus_publish_failed_outer event=%s err=%r — projection lost, "
                "SoT already persisted",
                type(event).__name__, exc,
            )
