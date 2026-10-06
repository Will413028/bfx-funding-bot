"""Operator uncertainty adjudication: the request outbox and its daemon worker.

ADR D4' (2026-09-22 amendment to the account-isolated execution target): every
operator adjudication is written by the web API as a request row and applied by
a single daemon worker. The web API keeps zero write privilege on the ledger and
projection tables -- the process that applies an adjudication needs write access
to every read model it touches, and that process must be the account's writer,
not the control plane.

The queue, worker loop and outcome recording are the shared operator-request
contract (``operator_requests``); this module adds what adjudication means.
What a resolution validates and writes is the ledger's ``OperatorResolution`` port.

The validation is shared. The web API runs it before accepting a request
so an operator still sees ``stale_reconcile_fence`` and friends immediately; the
worker runs it again, authoritatively, under the account lock in the same
transaction as the append. A request can therefore still be rejected after it
was accepted -- a newer snapshot arrived, another request resolved the
uncertainty first -- and that outcome is recorded on the request row with the
same bounded code, never collapsed into one opaque conflict.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    OperatorAuthority,
    OperatorRequestWorker,
    Outcome,
    RequestRejected,
    insert_request,
    root_cause_name,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    OperatorResolution,
    QueuedResolution,
    RequestColumns,
    ResolutionIntent,
    Scope,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger import (
    ResolutionRejected as EvidenceRejected,
)


class ResolutionRejected(RequestRejected, EvidenceRejected):
    """A refused resolution: an operator-request outcome and a port refusal at once."""


class ResolutionRequestPending(ResolutionRejected):
    """Another, different request for this uncertainty is still waiting."""

    def __init__(self) -> None:
        super().__init__("resolution_request_pending")


@dataclass(frozen=True, slots=True)
class ResolutionScope:
    account_id: UUID
    environment: str


def request_evidence_ref(row: UncertaintyResolutionRequestRow) -> str:
    """The opaque reference the operator cited, rebuilt from whichever column the row carries."""
    if row.observation_id is not None:
        return observation_evidence_ref(row.observation_id)
    if row.reconcile_event_seq is None:
        raise ValueError("request has no evidence column")
    return str(row.reconcile_event_seq)


def intent_from_request(row: UncertaintyResolutionRequestRow) -> ResolutionIntent:
    return ResolutionIntent(
        uncertainty_id=row.uncertainty_id,
        action=row.action,  # type: ignore[arg-type]  # CHECK constraint bounds it
        evidence_ref=request_evidence_ref(row),
        operator_id=row.requested_by,
        reason=row.reason,
        venue_offer_id=row.venue_offer_id,
        decision=row.decision,
    )


def request_rejection(exc: EvidenceRejected) -> ResolutionRejected:
    """A port refusal as the operator-request outcome it is (never a fault)."""
    if isinstance(exc, ResolutionRejected):
        return exc
    return ResolutionRejected(exc.code, kind=exc.kind)


def _same_intent(
    row: UncertaintyResolutionRequestRow, intent: ResolutionIntent, columns: RequestColumns
) -> bool:
    return (
        row.action == intent.action
        and row.reconcile_event_seq is None
        and RequestColumns(row.observation_id) == columns
        and row.requested_by == intent.operator_id
        and row.venue_offer_id == intent.venue_offer_id
        and row.decision == intent.decision
        and row.reason == intent.reason
    )


class UncertaintyResolutionRequests:
    """Account-serialized request queue. Caller owns the transaction."""

    def __init__(
        self, scope: ResolutionScope, resolution: OperatorResolution | None = None
    ) -> None:
        # Reading the queue (get / pending_for / latest_for) needs no port; only
        # validating or applying a request does, and those callers pass one.
        self.scope = scope
        self._resolution = resolution

    @property
    def resolution(self) -> OperatorResolution:
        if self._resolution is None:
            raise RuntimeError("uncertainty_resolution_port_missing")
        return self._resolution

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

    def _columns(self, intent: ResolutionIntent) -> RequestColumns:
        try:
            return self.resolution.columns(intent.evidence_ref)
        except EvidenceRejected as exc:
            raise request_rejection(exc) from exc

    async def _pending_or_conflict(
        self, session: AsyncSession, intent: ResolutionIntent, columns: RequestColumns
    ) -> UncertaintyResolutionRequestRow | None:
        pending = (await self.pending_for(session, [intent.uncertainty_id])).get(
            intent.uncertainty_id
        )
        if pending is None:
            return None
        if _same_intent(pending, intent, columns):
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
        columns = self._columns(intent)
        pending = await self._pending_or_conflict(session, intent, columns)
        if pending is not None:
            return pending
        try:
            columns = await self.resolution.prepare(
                session, Scope(self.scope.account_id, self.scope.environment), intent,
                now_ms=now_ms,
            )
        except EvidenceRejected as exc:
            raise request_rejection(exc) from exc
        values = request_values(self.scope, intent, columns, now_ms=now_ms)
        if await insert_request(session, UncertaintyResolutionRequestRow, values):
            request_id = values["request_id"]
            assert isinstance(request_id, UUID)
            return await self.get(session, request_id)
        # A concurrent request won the pending slot: the same one is this
        # request, a different one is a conflict.
        pending = await self._pending_or_conflict(session, intent, columns)
        if pending is None:
            raise RuntimeError("uncertainty_resolution_request_refused")
        return pending


def request_values(
    scope: ResolutionScope, intent: ResolutionIntent, columns: RequestColumns, *, now_ms: int
) -> dict[str, object]:
    """Exactly the web API's granted columns (``REQUEST_COLUMNS``)."""
    return {
        "request_id": uuid4(),
        "exchange_account_id": scope.account_id,
        "deployment_environment": scope.environment,
        "uncertainty_id": intent.uncertainty_id,
        "action": intent.action,
        # Pre-switch evidence only; the epoch trigger refuses it under the ledger.
        "reconcile_event_seq": None,
        "observation_id": columns.observation_id,
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
        resolution: OperatorResolution,
    ) -> None:
        super().__init__(session_factory=session_factory, account_id=scope.account_id,
                         environment=scope.environment, authority=authority, clock=clock,
                         ownership=ownership, poll_interval_s=poll_interval_s)
        self.scope = scope
        self.requests = UncertaintyResolutionRequests(scope, resolution)

    async def apply(self, session: AsyncSession, row: UncertaintyResolutionRequestRow,
                    prepared: None) -> Outcome:
        try:
            await self.requests.resolution.apply(
                session, Scope(self.scope.account_id, self.scope.environment),
                QueuedResolution(
                    row.request_id, intent_from_request(row),
                    RequestColumns(row.observation_id),
                ),
                now_ms=self.clock(),
            )
        except EvidenceRejected as exc:
            raise request_rejection(exc) from exc
        return Outcome(APPLIED)

    def failure_reason(self, exc: BaseException) -> str:
        return "resolution_failed:" + root_cause_name(exc)


__all__ = [
    "ResolutionAction",
    "ResolutionIntent",
    "ResolutionRejected",
    "ResolutionRequestPending",
    "ResolutionScope",
    "UncertaintyResolutionRequests",
    "UncertaintyResolutionWorker",
    "intent_from_request",
    "request_rejection",
    "request_values",
]
