"""Frozen pre-S1-3c4a context algorithm for differential compatibility tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext
from bfx_funding_bot.modules.api.uncertainties import UncertaintyResolutionContext
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionRejected,
    ResolutionScope,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    attempt_from_row,
    match_attempt_to_snapshot,
)

RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"
_MAX_CANDIDATE_VENUE_OFFER_IDS = 16


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _unavailable_context(
    *,
    reconcile_event_seq: int | None = None,
    payload: Mapping[str, Any] | None = None,
    reason: str,
    candidate_count: int | None = None,
    candidate_venue_offer_ids: list[str] | None = None,
) -> UncertaintyResolutionContext:
    return UncertaintyResolutionContext(
        reconcile_event_seq=reconcile_event_seq,
        query_started_at_ms=_optional_int(
            payload.get("query_started_at_ms") if payload is not None else None
        ),
        query_finished_at_ms=_optional_int(
            payload.get("query_finished_at_ms") if payload is not None else None
        ),
        candidate_count=candidate_count,
        candidate_venue_offer_ids=candidate_venue_offer_ids or [],
        unavailable_reason=reason[:256],
    )


def _scope(context: ExchangeAccountContext) -> ResolutionScope:
    return ResolutionScope(
        account_id=context.exchange_account_id,
        environment=context.deployment_environment,
    )


async def fresh_reconcile(
    session: AsyncSession,
    scope: ResolutionScope,
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
            EventLogRow.exchange_account_id == scope.account_id,
            EventLogRow.deployment_environment == scope.environment,
            EventLogRow.event_type == RECONCILE_EVENT_TYPE,
        )
    )
    if reconcile is None or not isinstance(reconcile.payload, dict):
        raise ResolutionRejected("stale_reconcile_fence")
    opening = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == row.opened_event_seq,
            EventLogRow.exchange_account_id == scope.account_id,
            EventLogRow.deployment_environment == scope.environment,
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
            EventLogRow.exchange_account_id == scope.account_id,
            EventLogRow.deployment_environment == scope.environment,
            EventLogRow.event_type == RECONCILE_EVENT_TYPE,
        )
    )
    if latest_seq != reconcile_event_seq:
        raise ResolutionRejected("stale_reconcile_fence")
    latest_projected_snapshot_at = await session.scalar(
        select(func.max(PositionStateRow.last_venue_snapshot_at)).where(
            PositionStateRow.exchange_account_id == scope.account_id,
            PositionStateRow.deployment_environment == scope.environment,
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
    session: AsyncSession, scope: ResolutionScope, row: ExecutionUncertaintyRow
) -> SubmissionAttemptRow:
    if row.kind != "submit_outcome_unknown" or row.attempt_id is None:
        raise ResolutionRejected("resolution_action_not_supported")
    attempt = await session.scalar(
        select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.attempt_id == row.attempt_id,
            SubmissionAttemptRow.exchange_account_id == scope.account_id,
            SubmissionAttemptRow.deployment_environment == scope.environment,
            SubmissionAttemptRow.symbol == row.symbol,
        )
    )
    if attempt is None or attempt.outcome_kind != "unknown":
        raise ResolutionRejected("submission_attempt_not_resolvable")
    return attempt


async def baseline_context(
    session: AsyncSession,
    *,
    context: ExchangeAccountContext,
    row: ExecutionUncertaintyRow,
) -> UncertaintyResolutionContext:
    """Describe only actions provable from the latest authoritative snapshot."""
    if row.state != "open":
        return _unavailable_context(reason="uncertainty_not_open")
    latest = await session.scalar(
        select(EventLogRow)
        .where(
            EventLogRow.exchange_account_id == context.exchange_account_id,
            EventLogRow.deployment_environment == context.deployment_environment,
            EventLogRow.event_type == RECONCILE_EVENT_TYPE,
        )
        .order_by(EventLogRow.event_seq.desc())
        .limit(1)
    )
    if latest is None or not isinstance(latest.payload, dict):
        return _unavailable_context(reason="fresh_reconcile_required")

    latest_payload = latest.payload
    scope = _scope(context)
    try:
        payload = await fresh_reconcile(
            session,
            scope,
            row,
            reconcile_event_seq=latest.event_seq,
            require_history=row.kind == "submit_outcome_unknown",
        )
    except ResolutionRejected as exc:
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=latest_payload,
            reason=exc.code,
        )

    base = {
        "reconcile_event_seq": latest.event_seq,
        "query_started_at_ms": _optional_int(payload.get("query_started_at_ms")),
        "query_finished_at_ms": _optional_int(payload.get("query_finished_at_ms")),
    }
    if row.kind in {"unattributed_venue_offer", "unsupported_venue_exposure"}:
        return UncertaintyResolutionContext(**base)
    if row.kind != "submit_outcome_unknown":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="unsupported_uncertainty_kind",
        )

    try:
        attempt_row = await load_attempt(session, scope, row)
    except ResolutionRejected as exc:
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason=exc.code,
        )
    attempt = attempt_from_row(attempt_row)
    if attempt is None:
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="submission_attempt_not_resolvable",
        )
    match = match_attempt_to_snapshot(attempt, payload)
    candidate_ids = sorted({offer.venue_offer_id for offer in match.candidates})
    bounded_candidate_ids = candidate_ids[:_MAX_CANDIDATE_VENUE_OFFER_IDS]
    if match.kind == "incomplete":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="incomplete_match_evidence",
        )
    if match.kind == "multiple_match":
        return _unavailable_context(
            reconcile_event_seq=latest.event_seq,
            payload=payload,
            reason="multiple_exact_candidates",
            candidate_count=len(candidate_ids),
            candidate_venue_offer_ids=bounded_candidate_ids,
        )
    return UncertaintyResolutionContext(
        **base,
        candidate_count=len(candidate_ids),
        candidate_venue_offer_ids=bounded_candidate_ids,
    )
