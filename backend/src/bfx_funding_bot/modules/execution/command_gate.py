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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import NoReturn, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.external.bitfinex.funding_rules import validate_amount
from bfx_funding_bot.external.bitfinex.live_executor import (
    format_offer_amount,
    format_venue_decimal,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprint_of
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
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
from bfx_funding_bot.modules.execution.legacy_command_journal import LegacyCommandJournal
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
    CommandJournal,
    CommandOutcome,
    CommandRefused,
    ManagedOfferReader,
    OutcomeKind,
    Scope,
    UncertaintyReader,
)
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

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
        bus: DomainEventBus,
        persister: EventPersister,
        uncertainty_reader: UncertaintyReader,
        safety_evaluator: AuthoritativeSafetyEvaluator,
        deployment_environment: str,
        is_simulated: bool = True,
        clock: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
        uncertainty_handler: Callable[[ReservationUnknown], Awaitable[None]] | None = None,
        capital_runtime: CapitalRuntime | None = None,
        managed_offers: ManagedOfferReader | None = None,
        command_journal: CommandJournal | None = None,
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
        if not is_simulated and capital_runtime is None:
            raise ValueError("live command gate requires applied capital runtime")
        self._capital = capital_runtime
        if capital_runtime is not None and managed_offers is None:
            raise ValueError("live command gate requires managed offer reads")
        self._offers = managed_offers
        self._journal = command_journal or (LegacyCommandJournal(
            capital_runtime, date_provider=self._date_provider, clock=self._clock,
            uncertainty_reader=self._uncertainty_reader,
        ) if capital_runtime is not None else None)
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
        cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        del cid  # This boundary is the sole CID authority.
        account_id = _canonical_account_id(context.account_id)
        lock_key = (str(account_id), self._deployment_environment)
        lock = self._account_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            self._admit("submit")
            if self._capital is None:
                await self.check(ready, context)
                await self._guard(ready.decision, context)
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
        command_date = self._date_provider()
        cid = generate_cid(decision.signal_correlation_id, command_date)
        reference = reservation_ref or ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=cid,
            signal_correlation_id=decision.signal_correlation_id,
        )
        _validate_reference(reference, ready=ready, cid=cid)
        size = decision.offer_amount_usdt if decision.offer_amount_usdt is not None else Decimal(0)
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
        assert isinstance(attempt.attempt_id, UUID)
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
        if self._capital is None:
            await self._persister.persist(intent)
        else:
            view = ready.capital_view
            if view is None:
                raise CommandGateBlocked("capital_authority_missing")
            runtime = self._capital
            if (str(runtime.repository.account_id), runtime.repository.environment) != (
                canonical_account, self._deployment_environment,
            ):
                raise CommandGateBlocked("capital_scope_conflict")
            async with runtime.session_factory.begin() as session:
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
                    assert self._offers is not None  # required with capital_runtime
                    if fingerprint in await self._offers.fingerprints_in_use(
                        locked, Scope(account_id, self._deployment_environment),
                        decision.symbol,
                    ):
                        raise CommandGateBlocked("amount_fingerprint_collision")
                    await self._guard(decision, replace(
                        context, command_session=locked, capital_cell_id=row.cell_id,
                    ))

                assert self._journal is not None
                admission = await self._journal.authorize(
                    session, Scope(account_id, self._deployment_environment),
                    CommandAttempt(
                        attempt.attempt_id, ready.decision_id, decision.symbol,
                        dict(attempt.normalized_payload), size, intent_ms, view.applied.revision,
                        view.applied.digest, view.applied.revision_id,
                        command_date=command_date,
                        event_id=intent.event_id,
                        cell_id=row.cell_id,
                    ), view.basis_token, now_ms=self._clock(), locked_guard=locked_guard,
                )
                if isinstance(admission, CommandRefused):
                    reason = {"revision_changed": "capital_policy_revision_changed",
                              "snapshot_changed": "capital_snapshot_changed"}.get(
                                  admission.reason, admission.reason)
                    raise CommandGateBlocked(reason)
        try:
            # Recheck ownership/halt after commit; never charge the reserved amount twice.
            try:
                if self._capital is not None:
                    await self._guard(decision, context, transport=True)
                    if not ready.book_valid_at(self._clock()):
                        raise CommandGateBlocked("decision_book_invalid_or_expired")
                    try:
                        validate_amount(size, ready.funding_amount_evidence,
                                        symbol=decision.symbol, now_ms=self._clock())
                    except (ValueError, ArithmeticError) as exc:
                        raise CommandGateBlocked(str(exc)) from exc
            except CommandGateBlocked as exc:
                result = SubmittedOrder(cid=cid, venue_offer_id=None,
                    outcome=SubmitNotSent(reason=str(exc)), reservation_ref=reference)
            else:
                result = await self._inner.submit(
                    ready, replace(context, before_submit_transport=(
                        lambda: ready.book_valid_at(self._clock())
                    )) if self._capital is not None else context,
                    cid=cid, reservation_ref=reference,
                )
        except SubmitCancelledNotSent as cancelled:
            # Cancelled before anything reached the venue: close the intent as
            # NOT_SENT so recovery need not escalate it, then keep cancelling.
            not_sent = SubmittedOrder(cid=cid, venue_offer_id=None,
                                      outcome=cancelled.outcome, reservation_ref=reference)
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
        if self._capital is not None:
            runtime = self._capital
            async def active(session: AsyncSession) -> bool:
                return await session.scalar(select(ExchangeAccount.lifecycle_status).where(
                    ExchangeAccount.id == runtime.repository.account_id,
                )) == "active"
            if context.command_session is not None:
                account_active = await active(context.command_session)
            else:
                async with self._capital.session_factory() as session:
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
        runtime = self._capital
        if runtime is None or not isinstance(self._inner, CancelPort):
            raise CommandGateBlocked("durable_cancel_unavailable")
        if runtime.repository.environment != self._deployment_environment:
            raise CommandGateBlocked("cancel_environment_conflict")
        if account_id != ctx.account_id or account_id != str(runtime.repository.account_id):
            raise CommandGateBlocked("cancel_account_conflict")
        lock = self._account_locks.setdefault((account_id, self._deployment_environment), asyncio.Lock())
        async with lock:
            self._admit("cancel")
            scope = Scope(runtime.repository.account_id, self._deployment_environment)
            probe: DecisionPayload | None = None

            async def locked_guard(locked: AsyncSession, admission: CancelAdmitted) -> None:
                nonlocal probe
                provenance = admission.provenance
                probe = DecisionPayload(decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=UUID(provenance.signal_correlation_id), symbol=provenance.symbol,
                    offer_amount_usdt=admission.amount, offer_rate=admission.rate,
                    offer_duration_days=admission.period_days)
                await self._guard(probe, replace(ctx, command_session=locked), cancel=True)

            async with runtime.session_factory.begin() as session:
                assert self._journal is not None
                admission = await self._journal.admit_cancel(
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
                reason=_bounded_reason(
                    result.outcome,
                    "submit_outcome_unknown",
                    secrets=(
                        context.credentials.api_key,
                        context.credentials.api_secret,
                    ),
                ),
            )
            await self._record_terminal(unknown_event, attempt_id)
            # Lending envelope D3 level 2: the open uncertainty quarantines this
            # symbol until a snapshot resolves it; nothing is halted.
            if self._uncertainty_handler is not None:
                await self._uncertainty_handler(unknown_event)
            return
        if result.outcome_kind is SubmitOutcomeKind.NOT_SENT:
            await self._record_terminal(
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
                ), attempt_id,
            )
            return
        if result.outcome_kind is SubmitOutcomeKind.REJECTED:
            await self._record_terminal(
                ReservationFailed(
                    cid=reference.cid,
                    size_usdt=size,
                    signal_correlation_id=decision.signal_correlation_id,
                    account_id=account_id,
                    is_simulated=self._is_simulated,
                    occurred_at_ms=occurred_at_ms,
                    symbol=decision.symbol,
                    reservation_ref=reference,
                    reason=_bounded_reason(
                        result.outcome,
                        "submit_rejected",
                        secrets=(
                            context.credentials.api_key,
                            context.credentials.api_secret,
                        ),
                    ),
                ), attempt_id,
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
                fill_rate=float(decision.offer_rate or 0),  # paper fill event field
            )
        events: tuple[object, ...] = (claimed, filled) if filled is not None else (claimed,)
        if self._journal is None:
            await self._persister.persist(*events)
        else:
            await self._record_terminal(claimed, attempt_id)
            if filled is not None:
                await self._persister.persist(filled)
        await self._safe_publish(claimed)
        if filled is not None:
            await self._safe_publish(filled)

    async def _record_terminal(
        self, event: ReservationClaimed | ReservationFailed | ReservationUnknown, attempt_id: UUID,
    ) -> None:
        if self._journal is None:
            await self._persister.persist(event)
            return
        kind: OutcomeKind = ("ack" if isinstance(event, ReservationClaimed) else
                "unknown" if isinstance(event, ReservationUnknown) else
                "not_sent" if event.reason == "local_pre_transport" else "rejected")
        await self._journal.record_outcome(
            Scope(_canonical_account_id(event.account_id), self._deployment_environment), attempt_id,
            CommandOutcome(kind, getattr(event, "venue_offer_id", None),
                           getattr(event, "reason", None), event.occurred_at_ms or 0, {},
                           event_id=event.event_id),
        )

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
