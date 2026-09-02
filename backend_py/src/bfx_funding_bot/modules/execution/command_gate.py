"""Fail-closed account command boundary for once-only venue writes.

The in-process lock spans guard evaluation, two durable database boundaries,
and the venue call. Database transactions never span the venue call: the
persister commits the intent and terminal outcome in two separate calls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
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
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    normalize_submit_payload,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

log = logging.getLogger(__name__)


class CommandGateBlocked(RuntimeError):  # noqa: N818 - domain state, not a failure suffix
    """The money command cannot safely proceed before creating an intent."""


class _OutcomeIdentityMismatchError(RuntimeError):
    """A venue result cannot safely be attributed to the intended command."""


class OpenUncertaintyReader(Protocol):
    async def has_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> bool: ...


class AuthoritativeSafetyEvaluator(Protocol):
    """Re-evaluate the complete safety chain at the locked money boundary."""

    async def evaluate(
        self,
        decision: DecisionPayload,
        context: AccountContext,
    ) -> GuardResult: ...


class DatabaseOpenUncertaintyReader:
    """Read the exact account/environment/symbol block from its projection."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def has_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutionUncertaintyRow.uncertainty_id).where(
                    ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                    ExecutionUncertaintyRow.deployment_environment
                    == deployment_environment,
                    ExecutionUncertaintyRow.symbol == symbol,
                    ExecutionUncertaintyRow.state == "open",
                ).limit(1)
            )
        return row is not None


class AccountCommandGate:
    """Serialize one account's submits and make ambiguous outcomes fail closed."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        bus: DomainEventBus,
        persister: EventPersister,
        uncertainty_reader: OpenUncertaintyReader,
        safety_evaluator: AuthoritativeSafetyEvaluator,
        deployment_environment: str,
        is_simulated: bool = True,
        clock: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
        uncertainty_handler: Callable[[ReservationUnknown], Awaitable[None]] | None = None,
    ) -> None:
        if not deployment_environment.strip():
            raise ValueError("deployment_environment must be non-empty")
        self._inner = inner
        self._bus = bus
        self._persister = persister
        self._uncertainty_reader = uncertainty_reader
        self._safety_evaluator = safety_evaluator
        self._deployment_environment = deployment_environment
        self._is_simulated = is_simulated
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._date_provider = date_provider or (lambda: datetime.now(UTC).date())
        self._uncertainty_handler = uncertainty_handler
        self._account_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._latched_scopes: dict[tuple[str, str, str], str] = {}

    async def check(
        self,
        decision: DecisionPayload | ReadyToSubmit,
        context: AccountContext,
    ) -> GuardResult:
        """Check the exact uncertainty scope, treating every read error as blocked."""
        payload = decision.decision if isinstance(decision, ReadyToSubmit) else decision
        account_id = _canonical_account_id(context.account_id)
        scope = (str(account_id), self._deployment_environment, payload.symbol)
        latched_reason = self._latched_scopes.get(scope)
        if latched_reason is not None:
            raise CommandGateBlocked(latched_reason)
        try:
            is_open = await self._uncertainty_reader.has_open(
                exchange_account_id=account_id,
                deployment_environment=self._deployment_environment,
                symbol=payload.symbol,
            )
        except Exception as exc:
            raise CommandGateBlocked("uncertainty guard unavailable") from exc
        if is_open:
            raise CommandGateBlocked("open execution uncertainty")
        return GuardResult(allowed=True, guard_name="account_command_gate")

    async def submit(
        self,
        ready: ReadyToSubmit,
        context: AccountContext,
        *,
        cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        del cid  # This boundary is the sole CID authority.
        account_id = _canonical_account_id(context.account_id)
        lock_key = (str(account_id), self._deployment_environment)
        lock = self._account_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            await self.check(ready, context)
            safety = await self._safety_evaluator.evaluate(ready.decision, context)
            if not safety.allowed:
                raise CommandGateBlocked(
                    safety.reason or f"guard blocked: {safety.guard_name}"
                )
            return await self._submit_locked(
                ready,
                context,
                reservation_ref=reservation_ref,
            )

    async def _submit_locked(
        self,
        ready: ReadyToSubmit,
        context: AccountContext,
        *,
        reservation_ref: ReservationRef | None,
    ) -> SubmittedOrder:
        decision = ready.decision
        account_id = _canonical_account_id(context.account_id)
        canonical_account = str(account_id)
        cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        reference = reservation_ref or ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=cid,
            signal_correlation_id=decision.signal_correlation_id,
        )
        _validate_reference(reference, ready=ready, cid=cid)
        size = Decimal(str(decision.offer_amount_usdt or 0.0))
        intent_ms = self._clock()
        attempt = SubmissionAttemptPayload(
            attempt_id=uuid4(),
            execution_decision_id=ready.decision_id,
            account_id=account_id,
            environment=self._deployment_environment,
            symbol=decision.symbol,
            cid=cid,
            normalized_payload=_normalized_venue_payload(decision),
            started_at_ms=intent_ms,
        )
        intent = ReservationIntent(
            cid=cid,
            size_usdt=size,
            signal_correlation_id=decision.signal_correlation_id,
            account_id=canonical_account,
            is_simulated=self._is_simulated,
            occurred_at_ms=intent_ms,
            symbol=decision.symbol,
            execution_decision_id=ready.decision_id,
            reservation_ref=reference,
            submission_attempt=attempt,
        )

        # Durable boundary 1. The call must finish before the venue sees data.
        await self._persister.persist(intent)
        try:
            result = await self._inner.submit(
                ready,
                context,
                cid=cid,
                reservation_ref=reference,
            )
        except BaseException:
            self._latch(decision.symbol, canonical_account, "submit ended without durable outcome")
            raise
        try:
            result = _bind_result(result, reference=reference)
        except _OutcomeIdentityMismatchError:
            # Transport completed, but the response cannot authoritatively be
            # assigned to this intent. Persist the only safe typed outcome.
            result = SubmittedOrder(
                cid=reference.cid,
                venue_offer_id=None,
                outcome=SubmitOutcomeUnknown(
                    reason="executor_result_identity_mismatch",
                    transport_started=True,
                ),
                reservation_ref=reference,
            )

        outcome_ms = self._clock()
        try:
            await self._persist_outcome(
                result,
                ready=ready,
                context=context,
                reference=result.reservation_ref or reference,
                size=size,
                occurred_at_ms=outcome_ms,
            )
        except BaseException:
            self._latch(decision.symbol, canonical_account, "outcome persistence failed")
            raise
        return result

    async def _persist_outcome(
        self,
        result: SubmittedOrder,
        *,
        ready: ReadyToSubmit,
        context: AccountContext,
        reference: ReservationRef,
        size: Decimal,
        occurred_at_ms: int,
    ) -> None:
        decision = ready.decision
        account_id = str(_canonical_account_id(context.account_id))
        if result.outcome_kind is SubmitOutcomeKind.UNKNOWN:
            unknown_event = ReservationUnknown(
                cid=reference.cid,
                size_usdt=size,
                signal_correlation_id=decision.signal_correlation_id,
                account_id=account_id,
                is_simulated=self._is_simulated,
                occurred_at_ms=occurred_at_ms,
                symbol=decision.symbol,
                reservation_ref=reference,
                reason=_bounded_reason(result.outcome, "submit_outcome_unknown"),
            )
            await self._persister.persist(unknown_event)
            if self._uncertainty_handler is not None:
                await self._uncertainty_handler(unknown_event)
            return
        if result.outcome_kind is SubmitOutcomeKind.NOT_SENT:
            await self._persister.persist(
                ReservationFailed(
                    cid=reference.cid,
                    size_usdt=size,
                    signal_correlation_id=decision.signal_correlation_id,
                    account_id=account_id,
                    is_simulated=self._is_simulated,
                    occurred_at_ms=occurred_at_ms,
                    symbol=decision.symbol,
                    reservation_ref=reference,
                    reason="local_pre_transport",
                )
            )
            return
        if result.outcome_kind is SubmitOutcomeKind.REJECTED:
            await self._persister.persist(
                ReservationFailed(
                    cid=reference.cid,
                    size_usdt=size,
                    signal_correlation_id=decision.signal_correlation_id,
                    account_id=account_id,
                    is_simulated=self._is_simulated,
                    occurred_at_ms=occurred_at_ms,
                    symbol=decision.symbol,
                    reservation_ref=reference,
                    reason=_bounded_reason(result.outcome, "submit_rejected"),
                )
            )
            return

        claimed = ReservationClaimed(
            cid=reference.cid,
            size_usdt=size,
            signal_correlation_id=decision.signal_correlation_id,
            account_id=account_id,
            is_simulated=self._is_simulated,
            occurred_at_ms=occurred_at_ms,
            symbol=decision.symbol,
            reservation_ref=reference,
            venue_offer_id=result.venue_offer_id or "",
        )
        filled: OrderFilled | None = None
        if result.status == "filled":
            filled = OrderFilled(
                cid=reference.cid,
                size_usdt=size,
                signal_correlation_id=decision.signal_correlation_id,
                account_id=account_id,
                is_simulated=self._is_simulated,
                occurred_at_ms=occurred_at_ms,
                symbol=decision.symbol,
                reservation_ref=reference,
                venue_offer_id=result.venue_offer_id or "",
                credit_id=None,
                fill_rate=decision.offer_rate or 0.0,
            )
        events: tuple[object, ...] = (claimed, filled) if filled is not None else (claimed,)
        await self._persister.persist(*events)
        await self._safe_publish(claimed)
        if filled is not None:
            await self._safe_publish(filled)

    def _latch(self, symbol: str, account_id: str, reason: str) -> None:
        self._latched_scopes[(account_id, self._deployment_environment, symbol)] = reason

    async def _safe_publish(self, event: object) -> None:
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical(
                "bus_publish_failed_outer event=%s err=%r — projection lost, SoT already persisted",
                type(event).__name__,
                exc,
            )


def _canonical_account_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CommandGateBlocked("account identity is not canonical") from exc


def _validate_reference(
    reference: ReservationRef,
    *,
    ready: ReadyToSubmit,
    cid: int,
) -> None:
    if (
        reference.execution_decision_id != ready.decision_id
        or reference.cid != cid
        or reference.signal_correlation_id != ready.decision.signal_correlation_id
        or reference.venue_offer_id is not None
    ):
        raise ValueError("reservation_ref conflicts with ReadyToSubmit request")


def _bind_result(
    result: SubmittedOrder,
    *,
    reference: ReservationRef,
) -> SubmittedOrder:
    if result.cid != reference.cid:
        raise _OutcomeIdentityMismatchError("executor returned a conflicting cid")
    returned = result.reservation_ref
    if returned is not None and (
        returned.execution_decision_id != reference.execution_decision_id
        or returned.cid != reference.cid
        or returned.signal_correlation_id != reference.signal_correlation_id
    ):
        raise _OutcomeIdentityMismatchError(
            "executor returned a reservation reference identity conflict"
        )
    if (
        returned is not None
        and returned.venue_offer_id is not None
        and returned.venue_offer_id != result.venue_offer_id
    ):
        raise _OutcomeIdentityMismatchError(
            "executor returned a reservation reference venue conflict"
        )
    if result.venue_offer_id is None:
        if returned is not None and returned.venue_offer_id is not None:
            raise _OutcomeIdentityMismatchError(
                "executor bound a venue id for a failed submit"
            )
        bound = reference
    else:
        bound = reference.bind_venue_offer(result.venue_offer_id)
    return replace(result, reservation_ref=bound)


def _normalized_venue_payload(decision: DecisionPayload) -> dict[str, object]:
    """Capture the exact secret-free funding-offer body before transport."""
    amount = (
        format(Decimal(str(decision.offer_amount_usdt)), "f")
        if decision.offer_amount_usdt is not None
        else None
    )
    rate = (
        format(Decimal(str(decision.offer_rate)), "f")
        if decision.offer_rate is not None
        else None
    )
    return normalize_submit_payload(
        {
            "type": "LIMIT",
            "symbol": decision.symbol,
            "amount": amount,
            "rate": rate,
            "period": decision.offer_duration_days,
            "flags": 0,
        }
    )


def _bounded_reason(outcome: object, fallback: str) -> str:
    value = getattr(outcome, "reason", fallback)
    text = str(value).strip() or fallback
    return text[:256]
