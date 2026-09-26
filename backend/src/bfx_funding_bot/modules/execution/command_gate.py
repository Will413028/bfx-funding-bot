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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.external.bitfinex.funding_rules import validate_amount
from bfx_funding_bot.external.bitfinex.live_executor import (
    format_offer_amount,
    format_venue_decimal,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.amount_fingerprint import (
    fingerprint_of,
    fingerprints_in_use,
)
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister
from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_stored_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, OfferClaimRow
from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
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
    SubmitNotSent,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    normalize_submit_payload,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.observability import alerts

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


class OpenUncertaintyReader(Protocol):
    async def has_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> bool: ...


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
        capital_runtime: CapitalRuntime | None = None,
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
        cid = generate_cid(decision.signal_correlation_id, self._date_provider())
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
            try:
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
                        if fingerprint in await fingerprints_in_use(
                            locked, account_id=account_id,
                            environment=self._deployment_environment, symbol=decision.symbol,
                        ):
                            raise CommandGateBlocked("amount_fingerprint_collision")
                        await self._guard(decision, replace(
                            context, command_session=locked, capital_cell_id=row.cell_id,
                        ))

                    await runtime.repository.authorize_and_append_intent(
                        session, intent=intent, decision=row,
                        expected_revision=view.applied.revision, expected_digest=view.applied.digest,
                        expected_snapshot_seq=view.snapshot_seq, now_ms=self._clock(),
                        locked_guard=locked_guard,
                    )
            except CapitalBlockedError as exc:
                reason = {"revision_changed": "capital_policy_revision_changed",
                          "snapshot_changed": "capital_snapshot_changed"}.get(str(exc), str(exc))
                raise CommandGateBlocked(reason) from exc
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
            async with runtime.session_factory.begin() as session:
                await runtime.repository.writer.prepare_locked(session, account_id=runtime.repository.account_id)
                claim = await session.scalar(select(OfferClaimRow).where(
                    OfferClaimRow.exchange_account_id == runtime.repository.account_id,
                    OfferClaimRow.deployment_environment == self._deployment_environment,
                    OfferClaimRow.venue_offer_id == venue_offer_id,
                ))
                if claim is None:
                    raise CommandGateBlocked("cancel_provenance_missing")
                evidence = await session.scalar(select(EventLogRow).where(
                    EventLogRow.exchange_account_id == runtime.repository.account_id,
                    EventLogRow.deployment_environment == self._deployment_environment,
                    EventLogRow.venue_offer_id == venue_offer_id,
                    EventLogRow.cid == claim.cid,
                    EventLogRow.event_type.in_(("RESERVATION_CLAIMED", "SUBMIT_MATCHED_TO_VENUE_OFFER")),
                ).order_by(EventLogRow.event_seq.desc()).limit(1))
                decision_row = await session.get(ExecutionDecisionRow, claim.execution_decision_id) if claim.execution_decision_id else None
                managed = deserialize_stored_event(evidence) if evidence is not None else None
                reference = getattr(managed, "reservation_ref", None)
                if (claim.state not in {"claimed", "released"} or reference is None
                    or decision_row is None
                    or decision_row.applied_rate is None or decision_row.duration_days is None
                    or (reference.execution_decision_id, reference.cid, reference.venue_offer_id,
                        str(reference.signal_correlation_id), getattr(managed, "symbol", None),
                        decision_row.exchange_account_id, decision_row.deployment_environment,
                        decision_row.symbol, decision_row.signal_correlation_id)
                    != (claim.execution_decision_id, claim.cid, venue_offer_id,
                        claim.signal_correlation_id, claim.symbol, runtime.repository.account_id,
                        self._deployment_environment, claim.symbol, claim.signal_correlation_id)):
                    raise CommandGateBlocked("cancel_provenance_conflict")
                uncertain = await session.scalar(select(ExecutionUncertaintyRow.uncertainty_id).where(
                    ExecutionUncertaintyRow.exchange_account_id == runtime.repository.account_id,
                    ExecutionUncertaintyRow.deployment_environment == self._deployment_environment,
                    ExecutionUncertaintyRow.symbol == claim.symbol,
                    ExecutionUncertaintyRow.state == "open",
                ).limit(1))
                if uncertain is not None:
                    raise CommandGateBlocked("cancel_provenance_uncertain")
                # Bind the durable cancel to the managed offer, not the current quote.
                signal_correlation_id = reference.signal_correlation_id
                # A cancel is a venue write, not a SKIP. Probe the managed
                # order's identity; explicitly omit only capital spending checks.
                probe = DecisionPayload(decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=signal_correlation_id, symbol=claim.symbol,
                    offer_amount_usdt=claim.size_usdt,
                    offer_rate=decision_row.applied_rate,
                    offer_duration_days=decision_row.duration_days)
                await self._guard(probe, replace(ctx, command_session=session), cancel=True)
                await runtime.repository.writer.append(session, CancelRequested(
                    venue_offer_id=venue_offer_id, requested_at_ms=self._clock(),
                    signal_correlation_id=signal_correlation_id, account_id=account_id,
                    occurred_at_ms=self._clock(),
                ))
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
            await self._persister.persist(unknown_event)
            # Lending envelope D3 level 2: the open uncertainty quarantines this
            # symbol until a snapshot resolves it; nothing is halted.
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
                    reason=_bounded_reason(
                        result.outcome,
                        "submit_rejected",
                        secrets=(
                            context.credentials.api_key,
                            context.credentials.api_secret,
                        ),
                    ),
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
                fill_rate=float(decision.offer_rate or 0),  # paper fill event field
            )
        events: tuple[object, ...] = (claimed, filled) if filled is not None else (claimed,)
        await self._persister.persist(*events)
        await self._safe_publish(claimed)
        if filled is not None:
            await self._safe_publish(filled)

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
