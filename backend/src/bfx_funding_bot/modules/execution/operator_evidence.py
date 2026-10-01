"""Legacy operator evidence: exact event fence and immutable attempt matching."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.operator_requests import (
    RequestRejected,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    attempt_from_row,
    match_attempt_to_snapshot,
)
from bfx_funding_bot.modules.ledger import (
    ResolutionEvidence,
    ResolutionSubject,
    Scope,
    VerifiedEvidence,
)
from bfx_funding_bot.modules.ledger import (
    ResolutionRejected as LedgerResolutionRejected,
)


class ResolutionRejected(RequestRejected, LedgerResolutionRejected):
    """Legacy rejection remains an operator-request outcome and a port refusal."""


RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"


def legacy_evidence_seq(token: str) -> int:
    if not (token.isascii() and token.isdigit()) or str(int(token)) != token:
        raise ResolutionRejected("stale_reconcile_fence")
    return int(token)


async def fresh_reconcile(
    session: AsyncSession,
    scope: Scope,
    row: ExecutionUncertaintyRow,
    *,
    reconcile_event_seq: int,
    require_history: bool,
) -> dict[str, Any]:
    if reconcile_event_seq <= row.opened_event_seq:
        raise ResolutionRejected("stale_reconcile_fence")
    reconcile = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == reconcile_event_seq,
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
            EventLogRow.event_type == RECONCILE_EVENT_TYPE,
        )
    )
    if reconcile is None or not isinstance(reconcile.payload, dict):
        raise ResolutionRejected("stale_reconcile_fence")
    opening = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == row.opened_event_seq,
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
        )
    )
    query_started_at_ms = reconcile.payload.get("query_started_at_ms")
    query_finished_at_ms = reconcile.payload.get("query_finished_at_ms")
    if (
        opening is None
        or not isinstance(query_started_at_ms, int)
        or isinstance(query_started_at_ms, bool)
        or not isinstance(query_finished_at_ms, int)
        or isinstance(query_finished_at_ms, bool)
        or query_started_at_ms <= opening.occurred_at_ms
        or query_finished_at_ms < query_started_at_ms
    ):
        raise ResolutionRejected("stale_reconcile_fence")
    # A sequence is a fence only if it is the newest snapshot currently known
    # for this exact account/environment.  This closes the race where an
    # operator submits an old complete snapshot after a newer partial read.
    latest_seq = await session.scalar(
        select(func.max(EventLogRow.event_seq)).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
            EventLogRow.event_type == RECONCILE_EVENT_TYPE,
        )
    )
    if latest_seq != reconcile_event_seq:
        raise ResolutionRejected("stale_reconcile_fence")
    latest_projected_snapshot_at = await session.scalar(
        select(func.max(PositionStateRow.last_venue_snapshot_at)).where(
            PositionStateRow.exchange_account_id == scope.exchange_account_id,
            PositionStateRow.deployment_environment == scope.deployment_environment,
        )
    )
    if (
        latest_projected_snapshot_at is not None
        and query_finished_at_ms < latest_projected_snapshot_at
    ):
        raise ResolutionRejected("stale_reconcile_fence")
    coverage = reconcile.payload.get("coverage")
    if not isinstance(coverage, dict) or not all(
        bool(coverage.get(key))
        for key in ("active_offers_complete", "active_credits_complete", "wallets_complete")
    ):
        raise ResolutionRejected("incomplete_reconcile_coverage")
    if require_history and not bool(coverage.get("offer_history_complete")):
        raise ResolutionRejected("incomplete_offer_history_coverage")
    return reconcile.payload


async def load_attempt(
    session: AsyncSession, scope: Scope, row: ExecutionUncertaintyRow
) -> SubmissionAttemptRow:
    if row.kind != "submit_outcome_unknown" or row.attempt_id is None:
        raise ResolutionRejected("resolution_action_not_supported")
    attempt = await session.scalar(
        select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.attempt_id == row.attempt_id,
            SubmissionAttemptRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptRow.deployment_environment == scope.deployment_environment,
            SubmissionAttemptRow.symbol == row.symbol,
        )
    )
    if attempt is None or attempt.outcome_kind != "unknown":
        raise ResolutionRejected("submission_attempt_not_resolvable")
    return attempt


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class LegacyOperatorEvidence:
    async def _subject(
        self, session: AsyncSession, scope: Scope, subject: ResolutionSubject
    ) -> ExecutionUncertaintyRow:
        row = await session.scalar(
            select(ExecutionUncertaintyRow)
            .where(
                ExecutionUncertaintyRow.uncertainty_id == subject.uncertainty_id,
                ExecutionUncertaintyRow.exchange_account_id == scope.exchange_account_id,
                ExecutionUncertaintyRow.deployment_environment == scope.deployment_environment,
                ExecutionUncertaintyRow.symbol == subject.symbol,
            )
            .execution_options(populate_existing=True)
        )
        if row is None or row.attempt_id != subject.attempt_id:
            raise ResolutionRejected("not_found", kind="not_found")
        return row

    async def verify(
        self,
        session: AsyncSession,
        scope: Scope,
        subject: ResolutionSubject,
        evidence_ref: str,
        *,
        require_history: bool,
    ) -> VerifiedEvidence:
        seq = legacy_evidence_seq(evidence_ref)
        row = await self._subject(session, scope, subject)
        payload = await fresh_reconcile(
            session, scope, row, reconcile_event_seq=seq, require_history=require_history
        )
        started = payload["query_started_at_ms"]
        finished = payload["query_finished_at_ms"]
        if row.kind != "submit_outcome_unknown":
            return VerifiedEvidence(evidence_ref, started, finished)
        attempt = attempt_from_row(await load_attempt(session, scope, row))
        if attempt is None:
            return VerifiedEvidence(evidence_ref, started, finished)
        match = match_attempt_to_snapshot(attempt, payload)
        ids = sorted({offer.venue_offer_id for offer in match.candidates})
        return VerifiedEvidence(
            evidence_ref,
            started,
            finished,
            tuple(ids[:16]),
            len(ids),
            match.kind,
            match.offer.venue_offer_id if match.offer is not None else None,
            match.offer.status if match.offer is not None else None,
        )

    async def resolution_context(
        self, session: AsyncSession, scope: Scope, subject: ResolutionSubject
    ) -> ResolutionEvidence:
        row = await self._subject(session, scope, subject)
        if row.state != "open":
            return ResolutionEvidence(unavailable_reason="uncertainty_not_open")
        latest = await session.scalar(
            select(EventLogRow)
            .where(
                EventLogRow.exchange_account_id == scope.exchange_account_id,
                EventLogRow.deployment_environment == scope.deployment_environment,
                EventLogRow.event_type == RECONCILE_EVENT_TYPE,
            )
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
        if latest is None or not isinstance(latest.payload, dict):
            return ResolutionEvidence(unavailable_reason="fresh_reconcile_required")
        base = ResolutionEvidence(
            str(latest.event_seq),
            _optional_int(latest.payload.get("query_started_at_ms")),
            _optional_int(latest.payload.get("query_finished_at_ms")),
        )
        try:
            verified = await self.verify(
                session,
                scope,
                subject,
                str(latest.event_seq),
                require_history=row.kind == "submit_outcome_unknown",
            )
        except ResolutionRejected as exc:
            return replace(base, unavailable_reason=exc.code[:256])
        if row.kind in {"unattributed_venue_offer", "unsupported_venue_exposure"}:
            return base
        if row.kind != "submit_outcome_unknown":
            return replace(base, unavailable_reason="unsupported_uncertainty_kind")
        if verified.match_kind is None:
            return replace(base, unavailable_reason="submission_attempt_not_resolvable")
        if verified.match_kind == "incomplete":
            return replace(base, unavailable_reason="incomplete_match_evidence")
        return replace(
            base,
            candidate_count=verified.candidate_count,
            candidate_venue_offer_ids=verified.candidate_venue_offer_ids,
            unavailable_reason="multiple_exact_candidates"
            if verified.match_kind == "multiple_match"
            else None,
        )
