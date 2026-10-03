"""Operator uncertainty-resolution requests under the ledger authority.

Two halves, as the request path itself is split. ``prepare`` runs in the web API's role,
which reads only column-allowlisted data: the subject is open, the action fits it, the
cited observation is the latest accepted one and began after the uncertainty opened, and
the operator's rule holds against a *preview* match computed from the generated match
columns (no payload, no digest recheck, so advisory). ``apply`` re-proves it all in the
daemon's locked transaction from the attempt's payload with its digest rechecked (the
authority), applies the same rule and writes the resolution journal row that the request
outcome then points at.

Operator parity with legacy: bind needs an exact match naming the requested offer,
not-accepted needs a zero match; no settle window, near-miss guard or attribution
(those are the automatic resolver's); ``multiple_match`` and ``incomplete`` never
resolve. A manual resolution is for quarantines only (the journal's rule).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    MANUAL_RESOLUTION_DECISIONS,
    AppliedResolution,
    JsonObject,
    OperatorEvidence,
    OperatorReads,
    QueuedResolution,
    RequestColumns,
    Resolution,
    ResolutionAction,
    ResolutionAlreadyRecorded,
    ResolutionIntent,
    ResolutionRejected,
    ResolutionSubject,
    Scope,
    UncertaintyView,
    UnknownMatch,
    VerifiedEvidence,
    match_unknown,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger._internal import resolver
from bfx_funding_bot.modules.ledger._internal.journal import record_resolution
from bfx_funding_bot.modules.ledger._internal.operator_evidence import (
    AttemptMatcher,
    LedgerOperatorEvidence,
    observation_id,
)
from bfx_funding_bot.modules.ledger.tables import (
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

_ACTIONS: dict[str, ResolutionAction] = {
    "bind_to_venue": "bound_to_venue",
    "mark_not_accepted": "not_accepted",
    "manual_resolution": "manual",
}


async def attempt_match(
    session: AsyncSession, scope: Scope, attempt_id: UUID, observation: UUID
) -> UnknownMatch | None:
    """The UNKNOWN attempt matched against one stored observation (pure ``match_unknown``).

    None when the attempt cannot be trusted as a subject (a seeded partial payload, a
    forged digest): legacy reads that as no match at all, so no verdict can rest on it.
    Reads the attempt payload, which the web API's role cannot: the worker's authority.
    """
    row = (
        await session.execute(
            select(
                SubmissionAttemptJournalRow.attempt_id,
                SubmissionAttemptJournalRow.symbol,
                SubmissionAttemptJournalRow.normalized_payload,
                SubmissionAttemptJournalRow.payload_sha256,
                SubmissionAttemptJournalRow.started_at_ms,
                TransportOutcomeJournalRow.completed_at_ms,
            )
            .join(
                TransportOutcomeJournalRow,
                TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
            )
            .where(
                SubmissionAttemptJournalRow.attempt_id == attempt_id,
                SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
                TransportOutcomeJournalRow.kind == "unknown",
            )
        )
    ).one_or_none()
    if row is None:
        return None
    terms = resolver._terms(*row)
    if terms is None:
        return None
    evidence = await resolver.load_match_evidence(session, observation)
    if evidence is None:
        raise ResolutionRejected("stale_reconcile_fence")
    return match_unknown(terms, evidence)


async def preview_match(
    session: AsyncSession, scope: Scope, attempt_id: UUID, observation: UUID
) -> UnknownMatch | None:
    """``attempt_match`` from granted columns only, so the web API's role can run it.

    The terms come from the generated match columns and the evidence from the granted
    observation columns: the payload digest is NOT rechecked and the payload's own symbol
    is not compared. Advisory (the operator's context and the queue-time check); the
    worker's ``attempt_match`` decides.
    """
    row = (
        await session.execute(
            select(
                SubmissionAttemptJournalRow.attempt_id,
                SubmissionAttemptJournalRow.symbol,
                SubmissionAttemptJournalRow.intended_amount,
                SubmissionAttemptJournalRow.match_rate,
                SubmissionAttemptJournalRow.match_period_days,
                SubmissionAttemptJournalRow.match_offer_type,
                SubmissionAttemptJournalRow.match_flags,
                SubmissionAttemptJournalRow.started_at_ms,
                TransportOutcomeJournalRow.completed_at_ms,
            )
            .join(
                TransportOutcomeJournalRow,
                TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
            )
            .where(
                SubmissionAttemptJournalRow.attempt_id == attempt_id,
                SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
                TransportOutcomeJournalRow.kind == "unknown",
            )
        )
    ).one_or_none()
    if row is None:
        return None
    terms = resolver.preview_terms(*row)
    if terms is None:
        return None
    evidence = await resolver.load_match_evidence(session, observation)
    if evidence is None:
        raise ResolutionRejected("stale_reconcile_fence")
    return match_unknown(terms, evidence)


def _verdict(intent: ResolutionIntent, match: UnknownMatch | None) -> tuple[str | None, int]:
    """The operator's rule over a match: ``(venue_offer_id, candidate_count)`` or a refusal.

    bind needs an exact match naming the requested offer; not-accepted a zero match.
    """
    if intent.action == "bind_to_venue":
        if (
            match is None
            or match.kind != "exact_match"
            or match.venue_offer_id is None
            or match.venue_status is None
            or intent.venue_offer_id != match.venue_offer_id
        ):
            raise ResolutionRejected("venue_offer_match_not_exact")
        return match.venue_offer_id, 1
    if match is None or match.kind != "zero_match":
        raise ResolutionRejected("venue_offer_match_not_zero")
    return None, 0


class LedgerOperatorResolution:
    def __init__(
        self, reads: OperatorReads, evidence: OperatorEvidence | None = None,
        *, preview: AttemptMatcher = preview_match, match: AttemptMatcher = attempt_match,
    ) -> None:
        self.reads = reads
        self.evidence = evidence if evidence is not None else LedgerOperatorEvidence(reads)
        self._preview = preview  # prepare: the web API's role, granted columns only
        self._match = match  # apply: the worker, payload and digest

    def columns(self, evidence_ref: str) -> RequestColumns:
        return RequestColumns(observation_id=observation_id(evidence_ref))

    async def _subject(
        self, session: AsyncSession, scope: Scope, intent: ResolutionIntent
    ) -> UncertaintyView:
        view = await self.reads.get_uncertainty(session, scope, intent.uncertainty_id)
        if view is None:
            raise ResolutionRejected("not_found", kind="not_found")
        if view.state != "open":
            raise ResolutionRejected("uncertainty_already_resolved")
        return view

    async def _check(
        self, session: AsyncSession, scope: Scope, intent: ResolutionIntent, evidence_ref: str
    ) -> tuple[UncertaintyView, VerifiedEvidence]:
        """Check order matches legacy, so the same request earns the same code."""
        view = await self._subject(session, scope, intent)
        subject = ResolutionSubject(view.uncertainty_id, view.symbol, view.attempt_id)
        if intent.action == "manual_resolution":
            if view.attempt_id is not None:
                raise ResolutionRejected("resolution_action_not_supported")
            if intent.decision not in MANUAL_RESOLUTION_DECISIONS:
                raise ResolutionRejected("invalid_manual_resolution_decision", kind="invalid")
            if not (intent.reason or "").strip():
                raise ResolutionRejected("operator_reason_required", kind="invalid")
            verified = await self.evidence.verify(
                session, scope, subject, evidence_ref, require_history=False
            )
        else:
            verified = await self.evidence.verify(
                session, scope, subject, evidence_ref, require_history=True
            )
            if view.attempt_id is None:
                raise ResolutionRejected("resolution_action_not_supported")
        return view, verified

    async def prepare(
        self, session: AsyncSession, scope: Scope, intent: ResolutionIntent, *, now_ms: int
    ) -> RequestColumns:
        del now_ms
        view, _ = await self._check(session, scope, intent, intent.evidence_ref)
        if intent.action != "manual_resolution":
            assert view.attempt_id is not None
            columns = self.columns(intent.evidence_ref)
            assert columns.observation_id is not None
            # Legacy refuses a wrong offer or a non-zero match when the request is queued.
            _verdict(intent, await self._preview(session, scope, view.attempt_id, columns.observation_id))
        return self.columns(intent.evidence_ref)

    async def apply(
        self, session: AsyncSession, scope: Scope, request: QueuedResolution, *, now_ms: int
    ) -> AppliedResolution:
        intent, observation = request.intent, request.columns.observation_id
        if observation is None:
            raise ValueError("ledger request has no observation_id")
        evidence_ref = observation_evidence_ref(observation)
        view, verified = await self._check(session, scope, intent, evidence_ref)
        detail: JsonObject = {
            "evidence_ref": evidence_ref,
            "query_started_at_ms": verified.query_started_at_ms,
            "query_finished_at_ms": verified.query_finished_at_ms,
        }
        venue_offer_id: str | None = None
        candidate_count: int | None = None
        if intent.action == "manual_resolution":
            reason = (intent.reason or "").strip()
            detail["decision"] = intent.decision
        else:
            assert view.attempt_id is not None
            match = await self._match(session, scope, view.attempt_id, observation)
            venue_offer_id, candidate_count = _verdict(intent, match)
            assert match is not None
            if intent.action == "bind_to_venue":
                detail["venue_offer_id"] = venue_offer_id
                detail["venue_status"] = match.venue_status
                reason = (intent.reason or "bind_to_venue_offer").strip()
            else:
                reason = (intent.reason or "confirmed_not_accepted").strip()
            detail["match_kind"] = match.kind
            detail["candidate_count"] = candidate_count
        resolution = Resolution(
            id=uuid4(), symbol=view.symbol, action=_ACTIONS[intent.action],
            venue_offer_id=venue_offer_id, observation_id=observation, actor_kind="operator",
            actor_id=intent.operator_id, resolved_at_ms=now_ms, reason=reason, evidence=detail,
            attempt_id=view.attempt_id,
            quarantine_id=view.uncertainty_id if view.attempt_id is None else None,
            operator_request_id=request.request_id, candidate_count=candidate_count,
        )
        try:
            await record_resolution(session, scope, resolution)
        except ResolutionAlreadyRecorded as exc:
            raise ResolutionRejected("uncertainty_already_resolved") from exc
        except ResolutionRejected as exc:
            # Verified moments ago under the same lock: the journal's own refusal is the
            # event-invariant case (legacy: ``resolution_event_invalid``).
            raise ResolutionRejected("resolution_event_invalid") from exc
        return AppliedResolution()
