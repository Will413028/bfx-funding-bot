"""Operator uncertainty adjudication: web API requests, daemon worker applies.

ADR D4' (2026-09-22 amendment to the account-isolated execution target): every
operator adjudication is written by the web API as a request row and applied by
a single daemon worker. The web API keeps zero write privilege on the ledger and
projection tables -- ``AccountEventWriter.append`` projects synchronously, so the
process that appends needs write access to every read model the event touches,
and that process must be the account's writer, not the control plane.

The queue, worker loop and outcome recording are the shared operator-request
contract (``operator_requests``); this module adds what adjudication means.

The validation below is shared. The web API runs it before accepting a request
so an operator still sees ``stale_reconcile_fence`` and friends immediately; the
worker runs it again, authoritatively, under the account lock in the same
transaction as the append. A request can therefore still be rejected after it
was accepted -- a newer snapshot arrived, another request resolved the
uncertainty first -- and that outcome is recorded on the request row with the
same bounded code, never collapsed into one opaque conflict.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
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
)
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    OperatorAuthority,
    OperatorRequestWorker,
    Outcome,
    insert_request,
    root_cause_name,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorEvidence,
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


class ResolutionRequestPending(ResolutionRejected):
    """Another, different request for this uncertainty is still waiting."""

    def __init__(self) -> None:
        super().__init__("resolution_request_pending")


@dataclass(frozen=True, slots=True)
class ResolutionScope:
    account_id: UUID
    environment: str


@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    """What the operator asked for; everything else is re-derived server side."""

    uncertainty_id: UUID
    action: ResolutionAction
    reconcile_event_seq: int
    operator_id: str
    reason: str | None = None
    venue_offer_id: str | None = None
    decision: str | None = None

    @classmethod
    def from_request(cls, row: UncertaintyResolutionRequestRow) -> ResolutionIntent:
        # Only legacy requests reach this reader; ledger-evidence rows carry no seq.
        if row.reconcile_event_seq is None:
            raise ValueError("request has no reconcile_event_seq")
        return cls(
            uncertainty_id=row.uncertainty_id,
            action=row.action,  # type: ignore[arg-type]  # CHECK constraint bounds it
            reconcile_event_seq=row.reconcile_event_seq,
            operator_id=row.requested_by,
            reason=row.reason,
            venue_offer_id=row.venue_offer_id,
            decision=row.decision,
        )


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
    account_id = str(scope.account_id)
    port = evidence if evidence is not None else LegacyOperatorEvidence()
    evidence_scope = Scope(scope.account_id, scope.environment)
    subject = ResolutionSubject(row.uncertainty_id, row.symbol, row.attempt_id)
    try:
        if intent.action == "bind_to_venue":
            verified = await port.verify(
                session, evidence_scope, subject, str(intent.reconcile_event_seq),
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
                reconcile_event_seq=intent.reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                venue_offer_id=intent.venue_offer_id,
                resolution_reason=(intent.reason or "bind_to_venue_offer").strip(),
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=intent.reconcile_event_seq,
                    verified=verified,
                    candidate_count=1,
                    venue_offer_id=intent.venue_offer_id,
                ),
                venue_status=verified.venue_status,
            )
        if intent.action == "mark_not_accepted":
            verified = await port.verify(
                session, evidence_scope, subject, str(intent.reconcile_event_seq),
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
                reconcile_event_seq=intent.reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                resolution_reason=(intent.reason or "confirmed_not_accepted").strip(),
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=intent.reconcile_event_seq,
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
                session, evidence_scope, subject, str(intent.reconcile_event_seq),
                require_history=False,
            )
            return UncertaintyManuallyResolved(
                uncertainty_id=row.uncertainty_id,
                account_id=account_id,
                environment=scope.environment,
                symbol=row.symbol,
                kind=row.kind,
                reconcile_event_seq=intent.reconcile_event_seq,
                resolved_by_operator_id=intent.operator_id,
                occurred_at_ms=occurred_at_ms,
                resolution_reason=(intent.reason or "").strip(),
                resolution_action=intent.decision,
                resolution_evidence=_resolution_audit(
                    reconcile_event_seq=intent.reconcile_event_seq,
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


def _same_intent(row: UncertaintyResolutionRequestRow, intent: ResolutionIntent) -> bool:
    return (
        row.action == intent.action
        and row.reconcile_event_seq == intent.reconcile_event_seq
        and row.requested_by == intent.operator_id
        and row.venue_offer_id == intent.venue_offer_id
        and row.decision == intent.decision
        and row.reason == intent.reason
    )


class UncertaintyResolutionRequests:
    """Account-serialized request queue. Caller owns the transaction."""

    def __init__(self, scope: ResolutionScope, evidence: OperatorEvidence | None = None) -> None:
        self.scope = scope
        self.evidence = evidence if evidence is not None else LegacyOperatorEvidence()

    async def get(self, session: AsyncSession, request_id: UUID) -> UncertaintyResolutionRequestRow:
        row = await session.scalar(
            select(UncertaintyResolutionRequestRow)
            .where(
                UncertaintyResolutionRequestRow.request_id == request_id,
                UncertaintyResolutionRequestRow.exchange_account_id == self.scope.account_id,
                UncertaintyResolutionRequestRow.deployment_environment == self.scope.environment,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise ResolutionRejected("not_found", kind="not_found")
        return row

    async def pending_for(
        self, session: AsyncSession, uncertainty_ids: list[UUID]
    ) -> dict[UUID, UncertaintyResolutionRequestRow]:
        if not uncertainty_ids:
            return {}
        rows = await session.scalars(
            select(UncertaintyResolutionRequestRow).where(
                UncertaintyResolutionRequestRow.exchange_account_id == self.scope.account_id,
                UncertaintyResolutionRequestRow.deployment_environment == self.scope.environment,
                UncertaintyResolutionRequestRow.uncertainty_id.in_(uncertainty_ids),
                UncertaintyResolutionRequestRow.state == "requested",
            )
        )
        return {row.uncertainty_id: row for row in rows}

    async def latest_for(
        self, session: AsyncSession, uncertainty_ids: list[UUID]
    ) -> dict[UUID, UncertaintyResolutionRequestRow]:
        """The newest request per uncertainty: pending, or the last outcome."""
        if not uncertainty_ids:
            return {}
        rows = await session.scalars(
            select(UncertaintyResolutionRequestRow)
            .where(
                UncertaintyResolutionRequestRow.exchange_account_id == self.scope.account_id,
                UncertaintyResolutionRequestRow.deployment_environment == self.scope.environment,
                UncertaintyResolutionRequestRow.uncertainty_id.in_(uncertainty_ids),
            )
            .order_by(
                UncertaintyResolutionRequestRow.created_at_ms.desc(),
                UncertaintyResolutionRequestRow.request_id.desc(),
            )
        )
        latest: dict[UUID, UncertaintyResolutionRequestRow] = {}
        for row in rows:
            latest.setdefault(row.uncertainty_id, row)
        return latest

    async def _pending_or_conflict(
        self, session: AsyncSession, intent: ResolutionIntent
    ) -> UncertaintyResolutionRequestRow | None:
        pending = (await self.pending_for(session, [intent.uncertainty_id])).get(
            intent.uncertainty_id
        )
        if pending is None:
            return None
        if _same_intent(pending, intent):
            return pending
        raise ResolutionRequestPending()

    async def request(
        self, session: AsyncSession, intent: ResolutionIntent, *, now_ms: int
    ) -> UncertaintyResolutionRequestRow:
        """Validate, then queue.

        Repeating an identical request while it waits returns the waiting row
        (a double click is not a second adjudication); a different request for
        the same uncertainty is refused until the first has an outcome.

        Deliberately takes no account lock: that lock serializes the daemon's
        writer, and holding it through validation and the HTTP response would
        stall reconcile and submit for as long as a request took. One pending
        request per uncertainty is the partial unique index's job, and the
        worker re-validates everything under the lock before appending.
        """
        pending = await self._pending_or_conflict(session, intent)
        if pending is not None:
            return pending
        await build_resolution_event(session, self.scope, intent, occurred_at_ms=now_ms,
                                     evidence=self.evidence)
        values = request_values(self.scope, intent, now_ms=now_ms)
        if await insert_request(session, UncertaintyResolutionRequestRow, values):
            request_id = values["request_id"]
            assert isinstance(request_id, UUID)
            return await self.get(session, request_id)
        # A concurrent request won the pending slot: the same one is this
        # request, a different one is a conflict.
        pending = await self._pending_or_conflict(session, intent)
        if pending is None:
            raise RuntimeError("uncertainty_resolution_request_refused")
        return pending


def request_values(
    scope: ResolutionScope, intent: ResolutionIntent, *, now_ms: int
) -> dict[str, object]:
    """Exactly the web API's granted columns (``REQUEST_COLUMNS``)."""
    return {
        "request_id": uuid4(),
        "exchange_account_id": scope.account_id,
        "deployment_environment": scope.environment,
        "uncertainty_id": intent.uncertainty_id,
        "action": intent.action,
        "reconcile_event_seq": intent.reconcile_event_seq,
        "observation_id": None,
        "venue_offer_id": intent.venue_offer_id,
        "decision": intent.decision,
        "reason": intent.reason,
        "requested_by": intent.operator_id,
        "created_at_ms": now_ms,
    }


class UncertaintyResolutionWorker(OperatorRequestWorker[UncertaintyResolutionRequestRow, None]):
    """Applies queued adjudications inside the account's single writer."""

    model = UncertaintyResolutionRequestRow
    name = "uncertainty_resolution"

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        scope: ResolutionScope,
        authority: OperatorAuthority,
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
        evidence: OperatorEvidence | None = None,
    ) -> None:
        super().__init__(session_factory=session_factory, account_id=scope.account_id,
                         environment=scope.environment, authority=authority, clock=clock,
                         ownership=ownership, poll_interval_s=poll_interval_s)
        self.scope = scope
        self.requests = UncertaintyResolutionRequests(scope, evidence)

    async def apply(self, session: AsyncSession, row: UncertaintyResolutionRequestRow,
                    prepared: None) -> Outcome:
        event = await build_resolution_event(
            session, self.scope, ResolutionIntent.from_request(row), occurred_at_ms=self.clock(),
            evidence=self.requests.evidence
        )
        result = await AccountEventWriter(
            store=PostgresEventStore(deployment_environment=self.scope.environment)
        ).append(session, event)
        return Outcome(APPLIED, columns={"resolved_event_seq": result.event_seq})

    def failure_reason(self, exc: BaseException) -> str:
        if isinstance(exc, ProjectionWriteError):
            return "projection_write_failed:" + root_cause_name(exc)
        return "resolution_failed:" + root_cause_name(exc)


__all__ = [
    "RECONCILE_EVENT_TYPE",
    "ResolutionAction",
    "ResolutionIntent",
    "ResolutionRejected",
    "ResolutionRequestPending",
    "ResolutionScope",
    "UncertaintyResolutionRequests",
    "UncertaintyResolutionWorker",
    "build_resolution_event",
    "load_open_uncertainty",
    "load_scoped_uncertainty",
    "request_values",
]
