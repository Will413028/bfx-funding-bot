"""Fail-closed account command boundary for once-only venue writes.

The in-process lock spans guard evaluation, two durable database boundaries,
and the venue call. Database transactions never span the venue call: the
persister commits the intent and terminal outcome in two separate calls.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from decimal import Decimal
from typing import NoReturn, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.funding_rules import validate_amount
from bfx_funding_bot.external.bitfinex.live_executor import (
    format_offer_amount,
    format_venue_decimal,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.command_boundary import CommandBoundary, CommandFacts
from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    CancelPort,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.safety.protection import ProtectionPort
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitCancelledNotSent,
    SubmitNotSent,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    normalize_submit_payload,
)
from bfx_funding_bot.modules.ledger import (
    CancelAdmitted,
    CommandAttempt,
    CommandOutcome,
    CommandRefused,
    ManagedOfferReader,
    Scope,
    UncertaintyReader,
)
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.trading import fingerprint_of

log = logging.getLogger(__name__)


class SubmitOutcomeLostError(BaseException):
    """A submit's outcome could not be made durable; the process must exit.

    A BaseException, like ``SystemExit``, so no ``except Exception`` on the way up
    keeps the daemon running: its task group ends and the container restarts.
    """


class CommandGateBlocked(RuntimeError):  # noqa: N818 - domain state, not a failure suffix
    """The money command cannot safely proceed before creating an intent."""


class _OutcomeIdentityMismatchError(RuntimeError):
    """A venue result cannot safely be attributed to the intended command."""


class CommandRateLimiter(Protocol):
    """Admits or refuses one venue write; never blocks (safety/pre_trade.CommandThrottle)."""

    def admit(self, kind: str) -> bool: ...


class AuthoritativeSafetyEvaluator(Protocol):
    """Re-evaluate the complete safety chain at the locked money boundary."""

    async def evaluate(
        self,
        decision: DecisionPayload,
        context: AccountContext,
    ) -> GuardResult: ...


class AccountCommandGate:
    """Serialize one account's submits and make ambiguous outcomes fail closed."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        uncertainty_reader: UncertaintyReader,
        safety_evaluator: AuthoritativeSafetyEvaluator,
        deployment_environment: str,
        boundary: CommandBoundary,
        managed_offers: ManagedOfferReader,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not deployment_environment.strip():
            raise ValueError("deployment_environment must be non-empty")
        self._inner = inner
        self._uncertainty_reader = uncertainty_reader
        self._safety_evaluator = safety_evaluator
        self._deployment_environment = deployment_environment
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._boundary = boundary
        self._offers = managed_offers
        # Automatic protections. ``trip`` only records and queues, so it is safe
        # to call here while this gate's account lock is held; the kill it
        # leads to waits for that lock from another task.
        self.protection: ProtectionPort | None = None
        # Always-on venue write rate limit (T9); installed by the live daemon.
        self.throttle: CommandRateLimiter | None = None
        self._account_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def quiesced(self, account_id: str, *, timeout_s: float) -> AsyncIterator[bool]:
        """Hold this account's command lock so no submit or cancel is mid-flight.

        The kill switch cancels everything at the venue; a submit already past
        its transport recheck could otherwise land after that. Waiting is
        bounded -- a wedged command must not hold a kill hostage -- and the
        caller learns whether the lock was obtained (``False`` on timeout).
        Never call this while holding the same lock: it is not re-entrant.
        """
        lock = self._account_locks.setdefault(
            (str(_canonical_account_id(account_id)), self._deployment_environment), asyncio.Lock(),
        )
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout_s)
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()

    async def check(
        self,
        decision: DecisionPayload | ReadyToSubmit,
        context: AccountContext,
    ) -> GuardResult:
        """Check the exact uncertainty scope, treating every read error as blocked."""
        payload = decision.decision if isinstance(decision, ReadyToSubmit) else decision
        account_id = _canonical_account_id(context.account_id)
        try:
            is_open = await self._uncertainty_reader.has_open(
                None, Scope(account_id, self._deployment_environment), payload.symbol,
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
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        # This boundary is the sole authority for a submit's reservation
        # reference: it derives one from ``ready`` and requires the venue
        # executor to echo it. A caller-supplied reference is a wiring error.
        if reservation_ref is not None:
            raise ValueError("reservation_ref is created by the command gate, not its caller")
        account_id = _canonical_account_id(context.account_id)
        lock_key = (str(account_id), self._deployment_environment)
        lock = self._account_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            self._admit("submit")
            return await self._submit_locked(ready, context)

    async def _submit_locked(
        self,
        ready: ReadyToSubmit,
        context: AccountContext,
    ) -> SubmittedOrder:
        decision = ready.decision
        account_id = _canonical_account_id(context.account_id)
        reference = ReservationRef(
            execution_decision_id=ready.decision_id,
            signal_correlation_id=decision.signal_correlation_id,
        )
        size = decision.offer_amount_usdt if decision.offer_amount_usdt is not None else Decimal(0)
        intent_ms = self._clock()
        attempt = SubmissionAttemptPayload(
            attempt_id=uuid4(),
            execution_decision_id=ready.decision_id,
            account_id=account_id,
            environment=self._deployment_environment,
            symbol=decision.symbol,
            normalized_payload=_normalized_venue_payload(decision),
            started_at_ms=intent_ms,
        )
        assert isinstance(attempt.attempt_id, UUID)

        # Durable boundary 1. The call must finish before the venue sees data.
        boundary = self._boundary
        view = ready.capital_view
        if view is None:
            raise CommandGateBlocked("capital_authority_missing")
        if boundary.scope != Scope(account_id, self._deployment_environment):
            raise CommandGateBlocked("capital_scope_conflict")
        async with boundary.session_factory.begin() as session:
            row = await session.get(ExecutionDecisionRow, ready.decision_id)
            if row is None or row.outcome != "ready" or (
                row.applied_rate != decision.offer_rate
                or row.duration_days != decision.offer_duration_days
            ):
                raise CommandGateBlocked("execution_audit_conflict")

            async def locked_guard(locked: AsyncSession) -> None:
                # D3a: the fingerprint is this submit's only identity at
                # the venue. Checked under the account lock, in the
                # transaction that writes the intent, so no two
                # unresolved submits of a symbol can ever share one.
                fingerprint = fingerprint_of(size)
                if not fingerprint:
                    raise CommandGateBlocked("amount_fingerprint_missing")
                assert self._offers is not None  # required with a boundary
                if fingerprint in await self._offers.fingerprints_in_use(
                    locked, Scope(account_id, self._deployment_environment),
                    decision.symbol,
                ):
                    raise CommandGateBlocked("amount_fingerprint_collision")
                await self._guard(decision, replace(
                    context, command_session=locked, capital_cell_id=row.cell_id,
                ))

            admission = await boundary.journal.authorize(
                session, boundary.scope,
                CommandAttempt(
                    attempt.attempt_id, ready.decision_id, decision.symbol,
                    dict(attempt.normalized_payload), size, intent_ms, view.applied.revision,
                    view.applied.digest, view.applied.revision_id,
                    event_id=boundary.effects.new_event_id(),
                    cell_id=row.cell_id,
                ), view.basis_token, now_ms=self._clock(), locked_guard=locked_guard,
            )
            if isinstance(admission, CommandRefused):
                raise CommandGateBlocked(admission.reason)
        try:
            # Recheck ownership/halt after commit; never charge the reserved amount twice.
            try:
                await self._guard(decision, context, transport=True)
                if not ready.book_valid_at(self._clock()):
                    raise CommandGateBlocked("decision_book_invalid_or_expired")
                try:
                    validate_amount(size, ready.funding_amount_evidence,
                                    symbol=decision.symbol, now_ms=self._clock())
                except (ValueError, ArithmeticError) as exc:
                    raise CommandGateBlocked(str(exc)) from exc
            except CommandGateBlocked as exc:
                result = SubmittedOrder(outcome=SubmitNotSent(reason=str(exc)),
                                        reservation_ref=reference)
            else:
                result = await self._inner.submit(
                    ready, replace(context, before_submit_transport=(
                        lambda: ready.book_valid_at(self._clock())
                    )),
                    reservation_ref=reference,
                )
        except SubmitCancelledNotSent as cancelled:
            # Cancelled before anything reached the venue: close the intent as
            # NOT_SENT so recovery need not escalate it, then keep cancelling.
            not_sent = SubmittedOrder(outcome=cancelled.outcome, reservation_ref=reference)
            try:
                await self._persist_outcome(
                    not_sent, ready=ready, context=context, reference=reference,
                    attempt_id=attempt.attempt_id,
                    size=size, occurred_at_ms=self._clock(),
                )
            except Exception as exc:  # recovery still resolves the PENDING intent
                log.warning("submit_not_sent_persist_failed symbol=%s err_type=%s",
                            decision.symbol, type(exc).__name__)
            raise
        except asyncio.CancelledError:
            raise  # shutting down: recovery turns the durable PENDING into UNKNOWN
        except BaseException as exc:
            self._outcome_lost(decision.symbol, "submit ended without durable outcome", exc)
        try:
            result = _bind_result(result, reference=reference)
        except _OutcomeIdentityMismatchError:
            # Transport completed, but the response cannot authoritatively be
            # assigned to this intent. Persist the only safe typed outcome.
            result = SubmittedOrder(
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
                attempt_id=attempt.attempt_id,
                ready=ready,
                context=context,
                reference=result.reservation_ref or reference,
                size=size,
                occurred_at_ms=outcome_ms,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._outcome_lost(decision.symbol, "outcome persistence failed", exc)
        return result

    async def _guard(self, decision: DecisionPayload, context: AccountContext,
                     *, transport: bool = False, cancel: bool = False) -> None:
        boundary = self._boundary

        async def active(session: AsyncSession) -> bool:
            return await session.scalar(select(ExchangeAccount.lifecycle_status).where(
                ExchangeAccount.id == boundary.scope.exchange_account_id,
            )) == "active"
        if context.command_session is not None:
            account_active = await active(context.command_session)
        else:
            async with boundary.session_factory() as session:
                account_active = await active(session)
        if not account_active:
            raise CommandGateBlocked("account_inactive")
        evaluate = self._safety_evaluator.evaluate
        if cancel:
            # Only an evaluator that knows which guards a cancel is exempt from
            # may admit one; falling back to the submit chain would block every
            # cancel under a stop, and guessing a subset here could skip more.
            cancel_evaluate = getattr(self._safety_evaluator, "evaluate_cancel", None)
            if cancel_evaluate is None:
                raise CommandGateBlocked("cancel_eligibility_unavailable")
            evaluate = cancel_evaluate
        elif transport:
            evaluate = getattr(self._safety_evaluator, "evaluate_transport", evaluate)
        result = await evaluate(decision, context)
        if not result.allowed:
            raise CommandGateBlocked(result.reason or f"guard blocked: {result.guard_name}")

    async def cancel(self, *, venue_offer_id: str, signal_correlation_id: UUID,
                     account_id: str, ctx: AccountContext) -> None:
        """Durable cancel admission; ACK never releases capital in this boundary.

        Cancelling is allowed in every trading state -- it is what HALTED is
        for -- so it is not gated on the trading state. It is still refused without managed provenance and
        while the offer's scope has an open or unreadable uncertainty, at
        admission and again before every transport attempt.
        """
        boundary = self._boundary
        if not isinstance(self._inner, CancelPort):
            raise CommandGateBlocked("durable_cancel_unavailable")
        if boundary.scope.deployment_environment != self._deployment_environment:
            raise CommandGateBlocked("cancel_environment_conflict")
        if account_id != ctx.account_id or account_id != str(boundary.scope.exchange_account_id):
            raise CommandGateBlocked("cancel_account_conflict")
        lock = self._account_locks.setdefault((account_id, self._deployment_environment), asyncio.Lock())
        async with lock:
            self._admit("cancel")
            scope = boundary.scope
            probe: DecisionPayload | None = None

            async def locked_guard(locked: AsyncSession, admission: CancelAdmitted) -> None:
                nonlocal probe
                provenance = admission.provenance
                probe = DecisionPayload(decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=UUID(provenance.signal_correlation_id), symbol=provenance.symbol,
                    offer_amount_usdt=admission.amount, offer_rate=admission.rate,
                    offer_duration_days=admission.period_days)
                await self._guard(probe, replace(ctx, command_session=locked), cancel=True)

            async with boundary.session_factory.begin() as session:
                admission = await boundary.journal.admit_cancel(
                    session, scope, venue_offer_id, now_ms=self._clock(), locked_guard=locked_guard,
                )
                if isinstance(admission, CommandRefused):
                    raise CommandGateBlocked(admission.reason)
                signal_correlation_id = UUID(admission.provenance.signal_correlation_id)
            assert probe is not None

            async def before_transport() -> None:
                # Fresh scoped uncertainty check after commit AND before
                # every idempotent retry. Read errors fail closed, outside txn.
                await self.check(probe, ctx)
                await self._guard(probe, ctx, cancel=True)

            await before_transport()
            await self._inner.cancel(venue_offer_id=venue_offer_id,
                signal_correlation_id=signal_correlation_id, account_id=account_id,
                ctx=replace(ctx, before_cancel_transport=before_transport))

    async def _persist_outcome(
        self,
        result: SubmittedOrder,
        *,
        attempt_id: UUID,
        ready: ReadyToSubmit,
        context: AccountContext,
        reference: ReservationRef,
        size: Decimal,
        occurred_at_ms: int,
    ) -> None:
        decision = ready.decision
        scope = Scope(_canonical_account_id(context.account_id), self._deployment_environment)
        secrets = (context.credentials.api_key, context.credentials.api_secret)
        kind = result.outcome_kind
        boundary = self._boundary
        event_id = boundary.effects.new_event_id()
        if kind is SubmitOutcomeKind.UNKNOWN:
            outcome = CommandOutcome(
                "unknown", None, _bounded_reason(result.outcome, "submit_outcome_unknown",
                                                 secrets=secrets),
                occurred_at_ms, {}, event_id=event_id)
        elif kind is SubmitOutcomeKind.NOT_SENT:
            outcome = CommandOutcome("not_sent", None, "local_pre_transport",
                                     occurred_at_ms, {}, event_id=event_id)
        elif kind is SubmitOutcomeKind.REJECTED:
            outcome = CommandOutcome(
                "rejected", None, _bounded_reason(result.outcome, "submit_rejected",
                                                  secrets=secrets),
                occurred_at_ms, {}, event_id=event_id)
        else:
            outcome = CommandOutcome("ack", result.venue_offer_id or "", None,
                                     occurred_at_ms, {}, event_id=event_id)
        facts = CommandFacts(
            scope=scope, attempt_id=attempt_id, symbol=decision.symbol, amount=size,
            signal_correlation_id=decision.signal_correlation_id, reference=reference,
            offer_rate=decision.offer_rate, is_simulated=False,
        )
        await boundary.journal.record_outcome(scope, attempt_id, outcome)
        await boundary.effects.outcome_recorded(facts, outcome)

    def _admit(self, kind: str) -> None:
        """Rate-limit venue writes before anything durable is written."""
        if self.throttle is not None and not self.throttle.admit(kind):
            raise CommandGateBlocked("command_rate_limited")

    def _outcome_lost(self, symbol: str, reason: str, exc: BaseException) -> NoReturn:
        """Process fencing (lending envelope D3): a submit whose outcome could
        not be made durable leaves this process unsure what it sent, so it stops
        writing and exits. The durable intent stays PENDING; the restarted
        daemon's recovery turns it into an UNKNOWN that quarantines the symbol
        until evidence resolves it -- no in-memory state to outlive the cause."""
        log.critical("submit_outcome_lost symbol=%s reason=%s error=%s: exiting", symbol, reason,
                     type(exc).__name__)
        alerts.emit(alerts.DAEMON_FATAL, error=f"{reason} ({symbol}): exiting")
        raise SubmitOutcomeLostError(f"{reason} ({symbol})") from exc


def _canonical_account_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CommandGateBlocked("account identity is not canonical") from exc


def _bind_result(
    result: SubmittedOrder,
    *,
    reference: ReservationRef,
) -> SubmittedOrder:
    """Accept a venue result only if it names this gate's own intent.

    The executor must echo the reference it was given: the execution decision
    id is 1:1 with the durable submission attempt, so a result without it, or
    with another decision's identity, cannot be attributed to this intent.
    """
    returned = result.reservation_ref
    if returned is None:
        raise _OutcomeIdentityMismatchError("executor returned no reservation reference")
    if (
        returned.execution_decision_id != reference.execution_decision_id
        or returned.signal_correlation_id != reference.signal_correlation_id
    ):
        raise _OutcomeIdentityMismatchError(
            "executor returned a reservation reference identity conflict"
        )
    if returned.venue_offer_id is not None and returned.venue_offer_id != result.venue_offer_id:
        raise _OutcomeIdentityMismatchError(
            "executor returned a reservation reference venue conflict"
        )
    if result.venue_offer_id is None:
        bound = reference
    else:
        bound = reference.bind_venue_offer(result.venue_offer_id)
    return replace(result, reservation_ref=bound)


def _normalized_venue_payload(decision: DecisionPayload) -> dict[str, object]:
    """Capture the exact secret-free funding-offer body before transport.

    Formatted by the venue adapter's own formatters from the decision's
    Decimals, so the durable attempt names byte-for-byte the amount and rate
    the venue receives (the amount's fingerprint included).
    """
    try:
        amount = (format_offer_amount(decision.offer_amount_usdt)
                  if decision.offer_amount_usdt is not None else None)
        rate = (format_venue_decimal(decision.offer_rate)
                if decision.offer_rate is not None else None)
    except ValueError as exc:
        raise CommandGateBlocked("offer_terms_invalid") from exc
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


def _bounded_reason(
    outcome: object,
    fallback: str,
    *,
    secrets: tuple[str, ...] = (),
) -> str:
    """Keep durable outcome evidence bounded and free of runtime credentials."""
    value = getattr(outcome, "reason", fallback)
    text = str(value).strip() or fallback
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)authorization\s*[:=]\s*\S+", "authorization=[redacted]", text)
    return text[:256]
