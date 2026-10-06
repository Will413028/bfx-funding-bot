"""CommandJournal backed by the existing serialized legacy writer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_policy_read import CapitalBlockedError
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_stored_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, OfferClaimRow
from bfx_funding_bot.modules.execution.events import (
    CancelRequested,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.legacy_ports import (
    LegacyUncertaintyReader,
    legacy_snapshot_seq,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.ledger import (
    Authorized,
    CancelAdmitted,
    CancelProvenance,
    CommandAttempt,
    CommandOutcome,
    CommandRefused,
    LockedCancelGuard,
    LockedCommandGuard,
    OutcomeAlreadyRecorded,
    OutcomeKind,
    Scope,
    UncertaintyReader,
)


class LegacyCommandJournal:
    def __init__(
        self, runtime: CapitalRuntime, *, date_provider: Callable[[], date],
        clock: Callable[[], int], uncertainty_reader: UncertaintyReader | None = None,
    ) -> None:
        self._runtime = runtime
        self._date_provider = date_provider
        self._clock = clock
        self._uncertainty = uncertainty_reader or LegacyUncertaintyReader(runtime.session_factory)

    def _scope(self, scope: Scope) -> None:
        if (scope.exchange_account_id, scope.deployment_environment) != (
            self._runtime.repository.account_id, self._runtime.repository.environment,
        ):
            raise ValueError("capital_scope_conflict")

    async def authorize(
        self, session: AsyncSession, scope: Scope, attempt: CommandAttempt,
        basis_token: str, *, now_ms: int, locked_guard: LockedCommandGuard,
    ) -> Authorized | CommandRefused:
        self._scope(scope)
        decision = await session.get(ExecutionDecisionRow, attempt.execution_decision_id)
        if decision is None:
            return CommandRefused("execution_audit_conflict")
        correlation = UUID(decision.signal_correlation_id)
        cid = generate_cid(correlation, attempt.command_date or self._date_provider())
        reference = ReservationRef(attempt.execution_decision_id, cid, correlation)
        submission = SubmissionAttemptPayload(
            attempt_id=attempt.attempt_id, execution_decision_id=attempt.execution_decision_id,
            account_id=scope.exchange_account_id, environment=scope.deployment_environment,
            symbol=attempt.symbol, cid=cid, normalized_payload=attempt.normalized_payload,
            started_at_ms=attempt.started_at_ms,
        )
        intent = ReservationIntent(
            cid=cid, size_usdt=attempt.amount,
            signal_correlation_id=correlation, account_id=str(scope.exchange_account_id),
            is_simulated=False, occurred_at_ms=attempt.started_at_ms, symbol=attempt.symbol,
            execution_decision_id=attempt.execution_decision_id, reservation_ref=reference,
            submission_attempt=submission,
            event_id=attempt.event_id or uuid4(),
        )
        try:
            result = await self._runtime.repository.authorize_and_append_intent(
                session, intent=intent, decision=decision,
                expected_revision=attempt.policy_revision, expected_digest=attempt.policy_digest,
                expected_snapshot_seq=legacy_snapshot_seq(basis_token), now_ms=now_ms,
                locked_guard=locked_guard,
            )
        except CapitalBlockedError as exc:
            return CommandRefused(str(exc))
        return Authorized(attempt.attempt_id, result.event_seq, submission.payload_fingerprint)

    async def _attempt(self, session: AsyncSession, scope: Scope, attempt_id: UUID) -> SubmissionAttemptRow:
        self._scope(scope)
        row = await session.get(SubmissionAttemptRow, attempt_id, populate_existing=True)
        if row is None:
            raise ValueError("attempt does not exist")
        if row.attempt_id != attempt_id:
            raise ValueError("attempt identity mismatch")
        if (row.exchange_account_id, row.deployment_environment) != (
            scope.exchange_account_id, scope.deployment_environment,
        ):
            raise ValueError("attempt scope mismatch")
        return row

    @staticmethod
    def _outcome(row: SubmissionAttemptRow) -> CommandOutcome | None:
        if row.outcome_kind is None:
            return None
        kind = "ack" if row.outcome_kind == "acknowledged" else row.outcome_kind
        return CommandOutcome(cast(OutcomeKind, kind), row.venue_offer_id, row.outcome_reason,
                              row.completed_at_ms or 0, {})

    async def read_back_outcome(self, scope: Scope, attempt_id: UUID) -> CommandOutcome | None:
        async with self._runtime.session_factory() as session:
            return await self._read_back(session, scope, attempt_id)

    async def _read_back(
        self, session: AsyncSession, scope: Scope, attempt_id: UUID,
    ) -> CommandOutcome | None:
        attempt = await self._attempt(session, scope, attempt_id)
        outcome = self._outcome(attempt)
        if outcome is None:
            return None
        event_row = await session.get(EventLogRow, attempt.last_event_seq)
        if event_row is None:
            raise ValueError("outcome evidence missing")
        event = deserialize_stored_event(event_row)
        reference = getattr(event, "reservation_ref", None)
        expected_type = {"ack": "RESERVATION_CLAIMED", "unknown": "SUBMIT_OUTCOME_UNKNOWN",
                         "not_sent": "RESERVATION_FAILED", "rejected": "RESERVATION_FAILED"}[outcome.kind]
        if event_row.event_type == "SUBMIT_MATCHED_TO_VENUE_OFFER" and outcome.kind == "ack":
            expected_type = event_row.event_type
        if (reference is None or event_row.event_type != expected_type
            or (event_row.exchange_account_id, event_row.deployment_environment,
                getattr(event, "symbol", None), getattr(event, "cid", None),
                reference.execution_decision_id, reference.venue_offer_id,
                getattr(event, "reason", None), getattr(event, "occurred_at_ms", None))
            != (scope.exchange_account_id, scope.deployment_environment, attempt.symbol,
                attempt.cid, attempt.execution_decision_id, outcome.venue_offer_id,
                outcome.reason, outcome.completed_at_ms)):
            raise ValueError("outcome identity mismatch")
        return replace(outcome, event_id=getattr(event, "event_id", None))

    async def record_outcome(self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome) -> None:
        async with self._runtime.session_factory.begin() as session:
            await self._runtime.repository.writer.prepare_locked(session, account_id=scope.exchange_account_id)
            attempt = await self._attempt(session, scope, attempt_id)
            stored = await self._read_back(session, scope, attempt_id)
            if stored is not None:
                raise OutcomeAlreadyRecorded(stored)
            decision = await session.get(ExecutionDecisionRow, attempt.execution_decision_id)
            if decision is None:
                raise ValueError("execution audit missing")
            correlation = UUID(decision.signal_correlation_id)
            intent = await session.scalar(select(EventLogRow).where(
                EventLogRow.exchange_account_id == scope.exchange_account_id,
                EventLogRow.deployment_environment == scope.deployment_environment,
                EventLogRow.cid == attempt.cid,
                EventLogRow.event_type == "RESERVATION_INTENT",
                EventLogRow.payload["execution_decision_id"].as_string() == attempt.execution_decision_id,
            ).order_by(EventLogRow.event_seq.desc()).limit(1))
            if intent is None:
                raise ValueError("attempt intent missing")
            reference = ReservationRef(attempt.execution_decision_id, attempt.cid, correlation,
                                       venue_offer_id=outcome.venue_offer_id)
            fields = {
                "cid": attempt.cid, "size_usdt": Decimal(str(intent.payload["amount"])),
                "signal_correlation_id": correlation, "account_id": str(scope.exchange_account_id),
                "is_simulated": False, "occurred_at_ms": outcome.completed_at_ms, "symbol": attempt.symbol,
                "reservation_ref": reference,
                "event_id": outcome.event_id or uuid4(),
            }
            event: ReservationClaimed | ReservationFailed | ReservationUnknown
            if outcome.kind == "ack":
                event = ReservationClaimed(**fields, venue_offer_id=outcome.venue_offer_id or "")  # type: ignore[arg-type]
            elif outcome.kind == "unknown":
                event = ReservationUnknown(**fields, reason=outcome.reason or "submit_outcome_unknown")  # type: ignore[arg-type]
            else:
                reason = "local_pre_transport" if outcome.kind == "not_sent" else outcome.reason or "submit_rejected"
                event = ReservationFailed(**fields, reason=reason)  # type: ignore[arg-type]
            await self._runtime.repository.writer.append(session, event)

    async def admit_cancel(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str,
        *, now_ms: int, locked_guard: LockedCancelGuard,
    ) -> CancelAdmitted | CommandRefused:
        self._scope(scope)
        await self._runtime.repository.writer.prepare_locked(session, account_id=scope.exchange_account_id)
        claim = await session.scalar(select(OfferClaimRow).where(
            OfferClaimRow.exchange_account_id == scope.exchange_account_id,
            OfferClaimRow.deployment_environment == scope.deployment_environment,
            OfferClaimRow.venue_offer_id == venue_offer_id,
        ))
        if claim is None:
            return CommandRefused("cancel_provenance_missing")
        evidence = await session.scalar(select(EventLogRow).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
            EventLogRow.venue_offer_id == venue_offer_id,
            EventLogRow.cid == claim.cid,
            EventLogRow.event_type.in_(("RESERVATION_CLAIMED", "SUBMIT_MATCHED_TO_VENUE_OFFER")),
        ).order_by(EventLogRow.event_seq.desc()).limit(1))
        decision = await session.get(ExecutionDecisionRow, claim.execution_decision_id) if claim.execution_decision_id else None
        managed = deserialize_stored_event(evidence) if evidence is not None else None
        reference = getattr(managed, "reservation_ref", None)
        if (claim.state not in {"claimed", "released"} or reference is None
            or decision is None or decision.applied_rate is None or decision.duration_days is None
            or (reference.execution_decision_id, reference.cid, reference.venue_offer_id,
                str(reference.signal_correlation_id), getattr(managed, "symbol", None),
                decision.exchange_account_id, decision.deployment_environment,
                decision.symbol, decision.signal_correlation_id)
            != (claim.execution_decision_id, claim.cid, venue_offer_id,
                claim.signal_correlation_id, claim.symbol, scope.exchange_account_id,
                scope.deployment_environment, claim.symbol, claim.signal_correlation_id)):
            return CommandRefused("cancel_provenance_conflict")
        if await self._uncertainty.has_open(session, scope, claim.symbol):
            return CommandRefused("cancel_provenance_uncertain")
        attempt_id = await session.scalar(select(SubmissionAttemptRow.attempt_id).where(
            SubmissionAttemptRow.execution_decision_id == claim.execution_decision_id,
        ))
        assert claim.execution_decision_id is not None
        admission = CancelAdmitted(
            CancelProvenance(venue_offer_id, claim.symbol, attempt_id,
                             claim.execution_decision_id, decision.cell_id, claim.signal_correlation_id),
            claim.size_usdt, decision.applied_rate, decision.duration_days,
        )
        await locked_guard(session, admission)
        await self._runtime.repository.writer.append(session, CancelRequested(
            venue_offer_id=venue_offer_id, requested_at_ms=now_ms,
            signal_correlation_id=reference.signal_correlation_id,
            account_id=str(scope.exchange_account_id), occurred_at_ms=self._clock(),
        ))
        return admission
