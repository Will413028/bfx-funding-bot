"""ReservationEmittingMiddleware — A2 write-ahead intent + sync SoT persistence.

Flow per submit:
  1. compute cid once (capture-once submit_date — INTENT/outcome share it)
  2. txn1: persist ReservationIntent (PENDING) — durable BEFORE venue submit
  3. inner.submit(cid) — the only non-transactional boundary (Bitfinex REST)
  4. txn2: persist typed outcome (CLAIMED[+ORDER_FILL] | FAILED | UNKNOWN)
  5. bus.publish CLAIMED[+FILL] for in-memory projections (ledger/registry) +
     diagnostics. INTENT/FAILED are NOT published (no in-memory subscriber).

Each persist() is its own txn (EventStorePersister) so a txn is never held
across the REST call. A crash between txn1 and txn2 leaves a durable PENDING
claim, resolved at boot (3a-recovery).

I3-EM: typed REJECTED/NOT_SENT -> persist FAILED; UNKNOWN gets a distinct
ReservationUnknown event and is never retried or published as a claim.
I4-EM: bus.publish raise -> swallow + log (persistence already committed).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    AuthoritativeSafetyEvaluator,
    DatabaseOpenUncertaintyReader,
)
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.event_store.persister import (
    CommandGatePersistence,
    EventPersister,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.ledger import ManagedOfferReader

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
        uncertainty_handler: Callable[[ReservationUnknown], Awaitable[None]] | None = None,
        safety_evaluator: AuthoritativeSafetyEvaluator | None = None,
        capital_runtime: CapitalRuntime | None = None,
        managed_offers: ManagedOfferReader | None = None,
    ) -> None:
        if not is_simulated and uncertainty_handler is None:
            raise ValueError(
                "live reservation middleware requires an uncertainty_handler"
            )
        self._inner = inner
        self._bus = bus
        self._persister = persister
        self._is_simulated = is_simulated
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._date_provider = date_provider or (lambda: datetime.now(UTC).date())
        self._uncertainty_handler = uncertainty_handler
        self._command_gate: AccountCommandGate | None = None
        capability = getattr(persister, "command_gate_persistence", None)
        if capability is not None and not isinstance(capability, CommandGatePersistence):
            raise TypeError("invalid durable command-gate capability")
        if not is_simulated and capability is None:
            raise ValueError("live middleware requires durable command-gate persistence")
        if not is_simulated and safety_evaluator is None:
            raise ValueError("live middleware requires authoritative safety evaluator")
        # Simulated unit adapters may intentionally retain the legacy path.
        # Every production persister with an injected safety chain uses the
        # serialized command boundary, including paper/shadow daemon modes.
        if capability is not None and safety_evaluator is not None:
            self._command_gate = AccountCommandGate(
                inner,
                bus=bus,
                persister=persister,
                uncertainty_reader=DatabaseOpenUncertaintyReader(
                    capability.session_factory
                ),
                safety_evaluator=safety_evaluator,
                deployment_environment=capability.store.deployment_environment,
                is_simulated=is_simulated,
                clock=self._clock,
                date_provider=self._date_provider,
                uncertainty_handler=uncertainty_handler,
                capital_runtime=capital_runtime,
                managed_offers=managed_offers,
            )

    @property
    def command_gate(self) -> AccountCommandGate | None:
        """Same daemon writer boundary used by an explicitly authorized release session."""
        return self._command_gate

    async def cancel(self, *, venue_offer_id: str, signal_correlation_id: UUID,
                     account_id: str, ctx: AccountContext) -> None:
        if self._command_gate is None:
            raise ValueError("durable cancel command gate required")
        await self._command_gate.cancel(venue_offer_id=venue_offer_id,
            signal_correlation_id=signal_correlation_id, account_id=account_id, ctx=ctx)

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        if self._command_gate is not None:
            return await self._command_gate.submit(
                ready,
                ctx,
                cid=cid,
                reservation_ref=reservation_ref,
            )
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
        size = decision.offer_amount_usdt if decision.offer_amount_usdt is not None else Decimal(0)
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

        returned_reference = result.reservation_ref
        if returned_reference is not None and (
            returned_reference.execution_decision_id != reference.execution_decision_id
            or returned_reference.cid != reference.cid
            or returned_reference.signal_correlation_id != reference.signal_correlation_id
        ):
            raise RuntimeError("executor returned a reservation reference identity conflict")
        if (
            returned_reference is not None
            and returned_reference.venue_offer_id is not None
            and returned_reference.venue_offer_id != result.venue_offer_id
        ):
            raise RuntimeError("executor returned a reservation reference venue conflict")
        if result.venue_offer_id is None:
            if returned_reference is not None and returned_reference.venue_offer_id is not None:
                raise RuntimeError("executor bound a venue id for a failed submit")
            bound_reference = reference
        else:
            # The adapter may bind the same immutable identity after venue ack.
            # Merge the acknowledged external id instead of comparing the full
            # dataclass, whose venue_offer_id is intentionally different.
            bound_reference = reference.bind_venue_offer(result.venue_offer_id)
        result = replace(result, reservation_ref=bound_reference)

        if result.outcome_kind is SubmitOutcomeKind.UNKNOWN:
            # Ambiguous transport outcomes must close the write-ahead intent
            # with a distinct pessimistic event.  Treating them as FAILED
            # would permit a duplicate venue offer on the next tick.
            unknown_reason = getattr(result.outcome, "reason", "submit_outcome_unknown")
            unknown_event = ReservationUnknown(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason=unknown_reason, occurred_at_ms=outcome_ms,
                symbol=decision.symbol, reservation_ref=bound_reference,
            )
            # This is a direct projection hook rather than a normal domain-bus
            # publication: UNKNOWN must never look like a venue claim, but the
            # live symbol gate must open before the next reconcile tick.
            await self._persister.persist(unknown_event)
            if self._uncertainty_handler is not None:
                await self._uncertainty_handler(unknown_event)
        elif result.outcome_kind is SubmitOutcomeKind.NOT_SENT:
            # Local validation happened before transport; it is safe to resolve
            # the intent as capital-neutral and it must not be labelled a venue
            # rejection.
            await self._persister.persist(ReservationFailed(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason="local_pre_transport", occurred_at_ms=outcome_ms,
                symbol=decision.symbol, reservation_ref=bound_reference,
            ))
        elif result.outcome_kind is SubmitOutcomeKind.ACKNOWLEDGED:
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
                    size_usdt=size, fill_rate=float(decision.offer_rate or 0),
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
        elif result.outcome_kind is SubmitOutcomeKind.REJECTED:
            # txn2: FAILED — reserved untouched
            await self._persister.persist(ReservationFailed(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason=getattr(result.outcome, "reason", "submit_failed"),
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
