"""The legacy event-log authority's uncertainty resolution.

``LegacyOperatorResolution`` is that authority's ``OperatorResolution`` port:
``build_resolution_event`` validates an operator's intent against current
evidence and returns the domain event, and ``AccountEventWriter.append``
projects it synchronously -- which is why only the account's writer (the
daemon worker in ``uncertainty_requests``) may apply it.

The validation is shared: the web API runs it before accepting a request, and
the worker runs it again, authoritatively, under the account lock in the same
transaction as the append.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
)
from bfx_funding_bot.modules.execution.operator_evidence import (
    RECONCILE_EVENT_TYPE,
    LegacyOperatorEvidence,
    ResolutionRejected,
    legacy_evidence_seq,
)
from bfx_funding_bot.modules.execution.uncertainty_requests import ResolutionScope
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.ledger import (
    AppliedResolution,
    OperatorEvidence,
    QueuedResolution,
    RequestColumns,
    ResolutionIntent,
    ResolutionSubject,
    Scope,
    VerifiedEvidence,
)
from bfx_funding_bot.modules.ledger import (
    ResolutionRejected as EvidenceRejected,
)

log = logging.getLogger(__name__)

SUPPORTED_KINDS = frozenset(
    {
        "submit_outcome_unknown",
        "unattributed_venue_offer",
        "unsupported_venue_exposure",
    }
)
_MANUAL_KINDS = frozenset({"unattributed_venue_offer", "unsupported_venue_exposure"})


async def load_scoped_uncertainty(
    session: AsyncSession, scope: ResolutionScope, uncertainty_id: UUID
) -> ExecutionUncertaintyRow:
    row = await session.scalar(
        select(ExecutionUncertaintyRow)
        .where(
            ExecutionUncertaintyRow.uncertainty_id == uncertainty_id,
            ExecutionUncertaintyRow.exchange_account_id == scope.account_id,
            ExecutionUncertaintyRow.deployment_environment == scope.environment,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        # Missing UUID, another account's UUID, and a retired/non-member account
        # all intentionally have the same non-enumerating response shape.
        raise ResolutionRejected("not_found", kind="not_found")
    return row


async def load_open_uncertainty(
    session: AsyncSession, scope: ResolutionScope, uncertainty_id: UUID
) -> ExecutionUncertaintyRow:
    row = await load_scoped_uncertainty(session, scope, uncertainty_id)
    if row.state != "open":
        raise ResolutionRejected("uncertainty_already_resolved")
    if row.kind not in SUPPORTED_KINDS:
        raise ResolutionRejected("unsupported_uncertainty_kind")
    return row


async def build_resolution_event(
    session: AsyncSession,
    scope: ResolutionScope,
    intent: ResolutionIntent,
    *,
    occurred_at_ms: int,
    evidence: OperatorEvidence | None = None,
) -> object:
    """Validate an intent against current evidence and return its domain event.

    Check order matches the synchronous endpoints this replaced, so the same
    request still earns the same code.
    """
    row = await load_open_uncertainty(session, scope, intent.uncertainty_id)
    reconcile_event_seq = legacy_evidence_seq(intent.evidence_ref)
    account_id = str(scope.account_id)
    port = evidence if evidence is not None else LegacyOperatorEvidence()
    evidence_scope = Scope(scope.account_id, scope.environment)
    subject = ResolutionSubject(row.uncertainty_id, row.symbol, row.attempt_id)
    try:
        if intent.action == "bind_to_venue":
            verified = await port.verify(
                session, evidence_scope, subject, intent.evidence_ref,
                require_history=True,
            )
            if row.kind != "submit_outcome_unknown" or row.attempt_id is None:
                raise ResolutionRejected("resolution_action_not_supported")
            if (
                verified.match_kind != "exact_match"
                or verified.venue_offer_id is None
                or verified.venue_status is None
                or intent.venue_offer_id is None
                or verified.venue_offer_id != intent.venue_offer_id
            ):
                raise ResolutionRejected("venue_offer_match_not_exact")
            return UncertaintyBoundToVenueOffer(
                uncertainty_id=row.uncertainty_id,
                account_id=account_id,
                environment=scope.environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                venue_offer_id=intent.venue_offer_id,
                resolution_reason=(intent.reason or "bind_to_venue_offer").strip(),
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=reconcile_event_seq,
                    verified=verified,
                    candidate_count=1,
                    venue_offer_id=intent.venue_offer_id,
                ),
                venue_status=verified.venue_status,
            )
        if intent.action == "mark_not_accepted":
            verified = await port.verify(
                session, evidence_scope, subject, intent.evidence_ref,
                require_history=True,
            )
            if row.kind != "submit_outcome_unknown":
                raise ResolutionRejected("resolution_action_not_supported")
            if verified.match_kind != "zero_match":
                raise ResolutionRejected("venue_offer_match_not_zero")
            return UncertaintyMarkedNotAccepted(
                uncertainty_id=row.uncertainty_id,
                account_id=account_id,
                environment=scope.environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                resolution_reason=(intent.reason or "confirmed_not_accepted").strip(),
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=reconcile_event_seq,
                    verified=verified,
                    candidate_count=0,
                ),
                candidate_count=0,
            )
        if intent.action == "manual_resolution":
            if row.kind not in _MANUAL_KINDS:
                raise ResolutionRejected("resolution_action_not_supported")
            if intent.decision not in MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS:
                raise ResolutionRejected("invalid_manual_resolution_decision", kind="invalid")
            if not (intent.reason or "").strip():
                raise ResolutionRejected("operator_reason_required", kind="invalid")
            verified = await port.verify(
                session, evidence_scope, subject, intent.evidence_ref,
                require_history=False,
            )
            return UncertaintyManuallyResolved(
                uncertainty_id=row.uncertainty_id,
                account_id=account_id,
                environment=scope.environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                resolution_reason=(intent.reason or "").strip(),
                resolution_action=intent.decision,
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=reconcile_event_seq,
                    verified=verified,
                ),
            )
    except EvidenceRejected as exc:
        if isinstance(exc, ResolutionRejected):
            raise
        raise ResolutionRejected(exc.code, kind=exc.kind) from exc
    except (ValueError, TypeError) as exc:
        # Event invariants (e.g. evidence shape) rejected the constructed event.
        raise ResolutionRejected("resolution_event_invalid") from exc
    raise ResolutionRejected("resolution_action_not_supported")


def _resolution_audit(*, reconcile_event_seq: int, verified: VerifiedEvidence,
                      candidate_count: int | None = None,
                      venue_offer_id: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "reconcile_event_seq": reconcile_event_seq,
        "query_started_at_ms": verified.query_started_at_ms,
        "query_finished_at_ms": verified.query_finished_at_ms,
    }
    if candidate_count is not None:
        result["candidate_count"] = candidate_count
    if venue_offer_id is not None:
        result["venue_offer_id"] = venue_offer_id
    return result


class LegacyOperatorResolution:
    """The event-log authority's request path: validated and applied as domain events."""

    def __init__(self, evidence: OperatorEvidence | None = None) -> None:
        self.evidence = evidence if evidence is not None else LegacyOperatorEvidence()

    def columns(self, evidence_ref: str) -> RequestColumns:
        return RequestColumns(reconcile_event_seq=legacy_evidence_seq(evidence_ref))

    async def prepare(
        self, session: AsyncSession, scope: Scope, intent: ResolutionIntent, *, now_ms: int
    ) -> RequestColumns:
        await build_resolution_event(
            session, ResolutionScope(scope.exchange_account_id, scope.deployment_environment),
            intent, occurred_at_ms=now_ms, evidence=self.evidence,
        )
        return self.columns(intent.evidence_ref)

    async def apply(
        self, session: AsyncSession, scope: Scope, request: QueuedResolution, *, now_ms: int
    ) -> AppliedResolution:
        event = await build_resolution_event(
            session, ResolutionScope(scope.exchange_account_id, scope.deployment_environment),
            request.intent, occurred_at_ms=now_ms, evidence=self.evidence,
        )
        result = await AccountEventWriter(
            store=PostgresEventStore(deployment_environment=scope.deployment_environment)
        ).append(session, event)
        return AppliedResolution(resolved_event_seq=result.event_seq)


__all__ = [
    "RECONCILE_EVENT_TYPE",
    "LegacyOperatorResolution",
    "build_resolution_event",
    "load_open_uncertainty",
    "load_scoped_uncertainty",
]
