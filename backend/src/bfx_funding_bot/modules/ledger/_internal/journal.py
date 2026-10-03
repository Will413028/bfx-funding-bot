"""First-write-only attempt, transport, and resolution journal operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Attempt,
    Authorized,
    AuthorizeRefused,
    CancelAdmitted,
    CommandAttempt,
    CommandRefused,
    JsonObject,
    LockedCancelGuard,
    LockedCommandGuard,
    Outcome,
    OutcomeAlreadyRecorded,
    OutcomeKind,
    ProvenanceConflict,
    RecordedAttempt,
    Resolution,
    ResolutionAction,
    ResolutionAlreadyRecorded,
    ResolutionRejected,
    Scope,
    encode_basis_token,
    parse_basis_token,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader
from bfx_funding_bot.modules.ledger._internal.clock import bump_locked, lock_scope
from bfx_funding_bot.modules.ledger._internal.operator_evidence import latest_accepted
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisRow,
    CapitalCommandClockRow,
    CapitalPolicyHeadRow,
    ExecutionResolutionJournalRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.trading import Blocked, CapitalScope


async def authorize_attempt(
    session: AsyncSession, scope: Scope, attempt: Attempt, basis_token: str, *, now_ms: int
) -> Authorized | AuthorizeRefused:
    return await _authorize_attempt(session, scope, attempt, basis_token, now_ms=now_ms)


async def _authorize_attempt(
    session: AsyncSession, scope: Scope, attempt: Attempt, basis_token: str, *, now_ms: int,
    locked_guard: LockedCommandGuard | None = None,
) -> Authorized | AuthorizeRefused:
    """CAS the read's query, clock and policy before inserting any intent.

    Capital eligibility/freshness and live guards belong to the command port;
    ``now_ms`` is its local clock, not an extra ledger freshness policy.
    """
    await lock_scope(session, scope)
    try:
        query_id, revision = parse_basis_token(basis_token)
    except ValueError:
        return AuthorizeRefused("capital_snapshot_changed")
    latest = await session.scalar(
        select(LedgerObservationQueryRow.query_id)
        .where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
    )
    if latest != query_id:
        return AuthorizeRefused("capital_snapshot_changed")
    basis_id = await session.scalar(
        select(AcceptedCapitalBasisRow.id)
        .join(
            LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id
        )
        .where(
            LedgerObservationRow.query_id == query_id,
            LedgerObservationRow.accepted.is_(True),
            AcceptedCapitalBasisRow.accepted.is_(True),
            AcceptedCapitalBasisRow.exchange_account_id == scope.exchange_account_id,
            AcceptedCapitalBasisRow.deployment_environment == scope.deployment_environment,
        )
    )
    if basis_id is None:
        return AuthorizeRefused("query_pending")
    clock = await session.scalar(
        select(CapitalCommandClockRow.revision).where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
    )
    if clock != revision or attempt.basis_id != basis_id:
        return AuthorizeRefused("capital_snapshot_changed")
    policy = await session.scalar(
        select(CapitalPolicyHeadRow.revision_id).where(
            CapitalPolicyHeadRow.exchange_account_id == scope.exchange_account_id,
            CapitalPolicyHeadRow.deployment_environment == scope.deployment_environment,
            CapitalPolicyHeadRow.symbol == attempt.symbol,
        )
    )
    if policy != attempt.policy_revision_id:
        return AuthorizeRefused("capital_policy_revision_changed")
    if locked_guard is not None:
        await locked_guard(session)
    recorded = await record_attempt(session, scope, attempt)
    return Authorized(recorded.attempt_id, recorded.attempt_seq, recorded.payload_sha256)


class _Stale:
    """The token's query or clock moved since the decision: the one retryable refusal."""


_STALE = _Stale()


async def authorize_command(
    session: AsyncSession, scope: Scope, attempt: CommandAttempt, basis_token: str, *,
    now_ms: int, locked_guard: LockedCommandGuard, max_snapshot_age_ms: int,
) -> Authorized | CommandRefused:
    """Scope lock -> CAS -> in-lock budget -> guard -> insert, retrying a stale token once.

    The retry re-reads capital under the same lock and transaction, so it never
    sees a state a concurrent writer can still change. Only a moved query/clock
    is retried; a pending query, a policy change, a blocked read or a budget
    shortfall is final, and so is a second stale answer.
    """
    if attempt.cell_id is None:
        return CommandRefused("execution_audit_conflict")
    await lock_scope(session, scope)
    first = await _command_pass(
        session, scope, attempt, basis_token, None, now_ms=now_ms,
        locked_guard=locked_guard, max_snapshot_age_ms=max_snapshot_age_ms,
    )
    if not isinstance(first, _Stale):
        return first
    fresh = await _read_budget(
        session, scope, attempt, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms
    )
    if isinstance(fresh, CommandRefused):
        return fresh
    second = await _command_pass(
        session, scope, attempt, fresh.token, fresh, now_ms=now_ms,
        locked_guard=locked_guard, max_snapshot_age_ms=max_snapshot_age_ms,
    )
    return CommandRefused("capital_snapshot_changed") if isinstance(second, _Stale) else second


@dataclass(frozen=True, slots=True)
class _Budget:
    token: str
    max_new_offer: Decimal


async def _read_budget(
    session: AsyncSession, scope: Scope, attempt: CommandAttempt, *, now_ms: int,
    max_snapshot_age_ms: int,
) -> _Budget | CommandRefused:
    """The attempt's cell budget, read inside the held lock (legacy: same lock, same order)."""
    assert attempt.cell_id is not None
    read = await capital_reader.read_capital_locked(
        session,
        CapitalScope(scope.exchange_account_id, scope.deployment_environment,
                     attempt.symbol, attempt.cell_id),
        now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
    )
    result = read.result
    if isinstance(result, Blocked):
        return CommandRefused(
            "query_pending" if result.reason == "snapshot_query_pending" else result.reason
        )
    view = result.view
    if view.applied.revision_id != attempt.policy_revision_id:
        return CommandRefused("capital_policy_revision_changed")
    assert read.query_id is not None and read.clock_revision is not None
    amount, budget = attempt.amount, view.budget
    if not amount.is_finite() or amount <= 0:
        return CommandRefused("intent_amount_conflict")
    if amount > budget.max_new_offer:
        return CommandRefused(budget.reason or "insufficient_deployable_funds")
    return _Budget(encode_basis_token(read.query_id, read.clock_revision), budget.max_new_offer)


async def _command_pass(
    session: AsyncSession, scope: Scope, attempt: CommandAttempt, basis_token: str,
    fresh: _Budget | None, *, now_ms: int, locked_guard: LockedCommandGuard,
    max_snapshot_age_ms: int,
) -> Authorized | CommandRefused | _Stale:
    """One CAS + budget + guard + insert against ``basis_token`` (``fresh``: retry pass)."""
    assert attempt.cell_id is not None
    resolved = await _resolve_token(session, scope, basis_token)
    if isinstance(resolved, AuthorizeRefused):
        return CommandRefused(resolved.reason)
    if isinstance(resolved, _Stale):
        return resolved
    basis_id, _ = resolved
    budget = fresh or await _read_budget(
        session, scope, attempt, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms
    )
    if isinstance(budget, CommandRefused):
        return budget
    evidence: JsonObject = {
        "basis_token": basis_token,
        "basis_id": str(basis_id),
        "max_new_offer": str(budget.max_new_offer),
        "retried": fresh is not None,
    }
    admitted = await _authorize_attempt(
        session, scope, Attempt(
            attempt.attempt_id, attempt.execution_decision_id, attempt.symbol, attempt.cell_id,
            attempt.normalized_payload, basis_id, attempt.policy_revision_id,
            evidence, attempt.started_at_ms,
        ), basis_token, now_ms=now_ms, locked_guard=locked_guard,
    )
    if isinstance(admitted, AuthorizeRefused):
        # The clock was checked above under the same lock, so only policy can differ here.
        return CommandRefused(admitted.reason)
    return admitted


async def _resolve_token(
    session: AsyncSession, scope: Scope, basis_token: str
) -> tuple[UUID, int] | AuthorizeRefused | _Stale:
    """(basis of the token's own query, clock) or why the token cannot be used."""
    try:
        query_id, revision = parse_basis_token(basis_token)
    except ValueError:
        return AuthorizeRefused("capital_snapshot_changed")  # a caller bug, not a race
    latest = await session.scalar(
        select(LedgerObservationQueryRow.query_id)
        .where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
    )
    if latest != query_id:
        return _STALE
    basis_id = await session.scalar(
        select(AcceptedCapitalBasisRow.id)
        .join(
            LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id
        )
        .where(
            LedgerObservationRow.query_id == query_id,
            LedgerObservationRow.accepted.is_(True),
            AcceptedCapitalBasisRow.accepted.is_(True),
            AcceptedCapitalBasisRow.exchange_account_id == scope.exchange_account_id,
            AcceptedCapitalBasisRow.deployment_environment == scope.deployment_environment,
        )
    )
    if basis_id is None:
        return AuthorizeRefused("query_pending")
    clock = await session.scalar(
        select(CapitalCommandClockRow.revision).where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
    )
    return _STALE if clock != revision else (basis_id, revision)


async def admit_cancel(
    session: AsyncSession, scope: Scope, venue_offer_id: str, *, now_ms: int,
    locked_guard: LockedCancelGuard,
) -> CancelAdmitted | CommandRefused:
    """Scope lock -> provenance -> uncertainty -> guard -> fence; no cancel row."""
    from bfx_funding_bot.modules.ledger._internal.reads import cancel_provenance, open_uncertainties

    await lock_scope(session, scope)
    try:
        provenance = await cancel_provenance(session, scope, venue_offer_id)
    except ProvenanceConflict:
        return CommandRefused("cancel_provenance_conflict")
    if provenance is None:
        return CommandRefused("cancel_provenance_missing")
    if await open_uncertainties(session, scope, provenance.symbol):
        return CommandRefused("cancel_provenance_uncertain")
    # Terms come from the attempt's normalized venue payload (what was sent),
    # not from execution's decision table.
    attempt = await session.get(SubmissionAttemptJournalRow, provenance.attempt_id)
    terms = _offer_terms(attempt.normalized_payload) if attempt is not None else None
    if terms is None:
        return CommandRefused("cancel_provenance_conflict")
    admission = CancelAdmitted(provenance, *terms)
    await locked_guard(session, admission)
    await bump_locked(session, scope)
    return admission


def _offer_terms(payload: dict[str, object]) -> tuple[Decimal, Decimal, int] | None:
    try:
        amount, rate = Decimal(str(payload["amount"])), Decimal(str(payload["rate"]))
        period = payload["period"]
    except (KeyError, InvalidOperation):
        return None
    if not (amount.is_finite() and rate.is_finite()) or not isinstance(period, int):
        return None
    return amount, rate, period


async def close_dangling(
    session: AsyncSession, scope: Scope, *, now_ms: int, grace_ms: int = 120_000
) -> tuple[UUID, ...]:
    """Crash-mid-flight has no durable rejection evidence: append UNKNOWN."""
    if grace_ms < 0:
        raise ValueError("grace must be nonnegative")
    await lock_scope(session, scope)
    dangling = list(
        await session.scalars(
            select(SubmissionAttemptJournalRow.attempt_id)
            .outerjoin(
                TransportOutcomeJournalRow,
                TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
            )
            .where(
                SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
                SubmissionAttemptJournalRow.started_at_ms <= now_ms - grace_ms,
                TransportOutcomeJournalRow.attempt_id.is_(None),
            )
            .order_by(SubmissionAttemptJournalRow.attempt_seq)
        )
    )
    for attempt_id in dangling:
        await record_outcome(
            session, scope, Outcome(attempt_id, "unknown", None, "unresolved_at_boot", now_ms, {})
        )
    return tuple(dangling)


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
    """Private unchecked insertion for cutover seeds/tests; runtime uses authorize_attempt."""
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
    # P3 (S1-3c4): only the scope's latest accepted observation may resolve.
    latest = await latest_accepted(session, scope)
    if not observation.accepted or latest is None or latest.id != observation.id:
        raise ResolutionRejected("observation is not the latest accepted observation")
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
