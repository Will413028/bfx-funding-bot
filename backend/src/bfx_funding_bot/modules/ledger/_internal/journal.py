"""First-write-only attempt, transport, and resolution journal operations."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Attempt,
    Outcome,
    OutcomeAlreadyRecorded,
    OutcomeKind,
    RecordedAttempt,
    Resolution,
    ResolutionAction,
    ResolutionAlreadyRecorded,
    ResolutionRejected,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal.clock import bump_locked, lock_scope
from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)


def canonical_payload(payload: dict[str, object]) -> bytes:
    """Pinned JSON encoding for the persisted payload digest."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


async def record_attempt(session: AsyncSession, scope: Scope, attempt: Attempt) -> RecordedAttempt:
    await lock_scope(session, scope)
    latest = await session.scalar(
        select(func.max(SubmissionAttemptJournalRow.attempt_seq)).where(
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
        )
    )
    payload = canonical_payload(attempt.normalized_payload)
    sequence = (latest or 0) + 1
    digest = sha256(payload).hexdigest()
    session.add(
        SubmissionAttemptJournalRow(
            attempt_id=attempt.attempt_id,
            execution_decision_id=attempt.execution_decision_id,
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
            symbol=attempt.symbol,
            cell_id=attempt.cell_id,
            attempt_seq=sequence,
            normalized_payload=json.loads(payload),
            payload_sha256=digest,
            basis_id=attempt.basis_id,
            policy_revision_id=attempt.policy_revision_id,
            authorization_evidence=attempt.authorization_evidence,
            seed_provenance=attempt.seed_provenance,
            started_at_ms=attempt.started_at_ms,
        )
    )
    await session.flush()
    await bump_locked(session, scope)
    return RecordedAttempt(attempt.attempt_id, sequence, digest)


def _outcome(row: TransportOutcomeJournalRow) -> Outcome:
    return Outcome(
        row.attempt_id,
        cast(OutcomeKind, row.kind),
        row.venue_offer_id,
        row.reason,
        row.completed_at_ms,
        row.evidence,
    )


async def read_back_outcome(session: AsyncSession, attempt_id: UUID) -> Outcome | None:
    row = await session.get(TransportOutcomeJournalRow, attempt_id)
    return None if row is None else _outcome(row)


async def record_outcome(session: AsyncSession, scope: Scope, outcome: Outcome) -> None:
    await lock_scope(session, scope)
    attempt = await session.get(SubmissionAttemptJournalRow, outcome.attempt_id)
    if attempt is None:
        raise ValueError("attempt does not exist")
    if (attempt.exchange_account_id, attempt.deployment_environment) != (
        scope.exchange_account_id,
        scope.deployment_environment,
    ):
        raise ValueError("attempt scope mismatch")
    stored = await read_back_outcome(session, outcome.attempt_id)
    if stored is not None:
        raise OutcomeAlreadyRecorded(stored)
    session.add(
        TransportOutcomeJournalRow(
            attempt_id=outcome.attempt_id,
            kind=outcome.kind,
            venue_offer_id=outcome.venue_offer_id,
            reason=outcome.reason,
            completed_at_ms=outcome.completed_at_ms,
            evidence=outcome.evidence,
        )
    )
    await session.flush()
    await bump_locked(session, scope)


def _resolution(row: ExecutionResolutionJournalRow) -> Resolution:
    return Resolution(
        id=row.id,
        symbol=row.symbol,
        action=cast(ResolutionAction, row.action),
        venue_offer_id=row.venue_offer_id,
        observation_id=row.observation_id,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        resolved_at_ms=row.resolved_at_ms,
        reason=row.reason,
        evidence=row.evidence,
        attempt_id=row.attempt_id,
        quarantine_id=row.quarantine_id,
        operator_request_id=row.operator_request_id,
        candidate_count=row.candidate_count,
    )


async def record_resolution(session: AsyncSession, scope: Scope, resolution: Resolution) -> None:
    await lock_scope(session, scope)
    if (resolution.attempt_id is None) == (resolution.quarantine_id is None):
        raise ResolutionRejected("exactly one subject is required")
    if resolution.attempt_id is not None and resolution.action == "manual":
        # An attempt is either bound to its venue offer or proven not accepted.
        raise ResolutionRejected("manual resolution applies to quarantines only")
    subject: SubmissionAttemptJournalRow | QuarantineOpeningRow | None
    if resolution.attempt_id is not None:
        subject = await session.get(SubmissionAttemptJournalRow, resolution.attempt_id)
        outcome = await read_back_outcome(session, resolution.attempt_id)
        if outcome is None or outcome.kind != "unknown":
            raise ResolutionRejected("attempt has no UNKNOWN outcome")
        existing = await session.scalar(
            select(ExecutionResolutionJournalRow).where(
                ExecutionResolutionJournalRow.attempt_id == resolution.attempt_id
            )
        )
        opening_ms = subject.started_at_ms if subject is not None else None
    else:
        subject = await session.get(QuarantineOpeningRow, resolution.quarantine_id)
        existing = await session.scalar(
            select(ExecutionResolutionJournalRow).where(
                ExecutionResolutionJournalRow.quarantine_id == resolution.quarantine_id
            )
        )
        opening_ms = subject.opened_at_ms if subject is not None else None
    if subject is None or (
        subject.exchange_account_id != scope.exchange_account_id
        or subject.deployment_environment != scope.deployment_environment
        or subject.symbol != resolution.symbol
    ):
        raise ResolutionRejected("subject scope mismatch")
    if existing is not None:
        raise ResolutionAlreadyRecorded(_resolution(existing))
    observation = await session.get(LedgerObservationRow, resolution.observation_id)
    if observation is None:
        raise ResolutionRejected("observation does not exist")
    query = await session.get(LedgerObservationQueryRow, observation.query_id)
    if (
        query is None
        or opening_ms is None
        or query.started_at_ms <= opening_ms
        or observation.exchange_account_id != scope.exchange_account_id
        or observation.deployment_environment != scope.deployment_environment
        or not all(
            (
                observation.wallets_complete,
                observation.offers_complete,
                observation.credits_complete,
                observation.loans_complete,
                observation.offer_history_complete,
                observation.credit_history_complete,
                observation.trades_complete,
            )
        )
        or observation.first_digest != observation.confirmation_digest
    ):
        raise ResolutionRejected("observation is stale, incomplete, or out of scope")
    session.add(
        ExecutionResolutionJournalRow(
            id=resolution.id,
            attempt_id=resolution.attempt_id,
            quarantine_id=resolution.quarantine_id,
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
            symbol=resolution.symbol,
            action=resolution.action,
            venue_offer_id=resolution.venue_offer_id,
            observation_id=resolution.observation_id,
            actor_kind=resolution.actor_kind,
            actor_id=resolution.actor_id,
            operator_request_id=resolution.operator_request_id,
            resolved_at_ms=resolution.resolved_at_ms,
            candidate_count=resolution.candidate_count,
            reason=resolution.reason,
            evidence=resolution.evidence,
        )
    )
    await session.flush()
    await bump_locked(session, scope)
