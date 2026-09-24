"""Operator uncertainty adjudication: web API requests, daemon worker applies.

ADR D4' (2026-09-22 amendment to the account-isolated execution target): every
operator adjudication is written by the web API as a request row and applied by
a single daemon worker. The web API keeps zero write privilege on the ledger and
projection tables -- ``AccountEventWriter.append`` projects synchronously, so the
process that appends needs write access to every read model the event touches,
and that process must be the account's writer, not the control plane.

The validation below is shared. The web API runs it before accepting a request
so an operator still sees ``stale_reconcile_fence`` and friends immediately; the
worker runs it again, authoritatively, under the account lock in the same
transaction as the append. A request can therefore still be rejected after it
was accepted -- a newer snapshot arrived, another request resolved the
uncertainty first -- and that outcome is recorded on the request row with the
same bounded code, never collapsed into one opaque conflict.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
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
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    ResolutionAction,
    SubmissionAttemptRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    attempt_from_row,
    deterministic_resolution_evidence,
    match_attempt_to_snapshot,
)

log = logging.getLogger(__name__)

RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"
SUPPORTED_KINDS = frozenset(
    {
        "submit_outcome_unknown",
        "unattributed_venue_offer",
        "unsupported_venue_exposure",
    }
)
_MANUAL_KINDS = frozenset({"unattributed_venue_offer", "unsupported_venue_exposure"})

RejectionKind = Literal["conflict", "invalid", "not_found"]


class ResolutionRejected(RuntimeError):  # noqa: N818 - domain outcome, not a fault
    """A bounded, operator-readable reason the adjudication cannot apply."""

    def __init__(self, code: str, *, kind: RejectionKind = "conflict") -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


class ResolutionRequestPending(ResolutionRejected):
    """Another, different request for this uncertainty is still waiting."""

    def __init__(self) -> None:
        super().__init__("resolution_request_pending")


class OperatorAuthority(Protocol):
    """Shared operator check; production passes ``release_worker.operator_authorized``."""

    async def __call__(self, session: AsyncSession, *, account_id: UUID, user: str) -> bool: ...


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


async def build_resolution_event(
    session: AsyncSession,
    scope: ResolutionScope,
    intent: ResolutionIntent,
    *,
    occurred_at_ms: int,
) -> object:
    """Validate an intent against current evidence and return its domain event.

    Check order matches the synchronous endpoints this replaced, so the same
    request still earns the same code.
    """
    row = await load_open_uncertainty(session, scope, intent.uncertainty_id)
    account_id = str(scope.account_id)
    try:
        if intent.action == "bind_to_venue":
            payload = await fresh_reconcile(
                session, scope, row,
                reconcile_event_seq=intent.reconcile_event_seq, require_history=True,
            )
            attempt = attempt_from_row(await load_attempt(session, scope, row))
            match = match_attempt_to_snapshot(attempt, payload) if attempt is not None else None
            if (
                match is None
                or match.kind != "exact_match"
                or match.offer is None
                or intent.venue_offer_id is None
                or match.offer.venue_offer_id != intent.venue_offer_id
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
                resolution_evidence=deterministic_resolution_evidence(
                    reconcile_event_seq=intent.reconcile_event_seq,
                    payload=payload,
                    candidate_count=1,
                    venue_offer_id=intent.venue_offer_id,
                ),
                venue_status=match.offer.status,
            )
        if intent.action == "mark_not_accepted":
            payload = await fresh_reconcile(
                session, scope, row,
                reconcile_event_seq=intent.reconcile_event_seq, require_history=True,
            )
            if row.kind != "submit_outcome_unknown":
                raise ResolutionRejected("resolution_action_not_supported")
            attempt = attempt_from_row(await load_attempt(session, scope, row))
            match = match_attempt_to_snapshot(attempt, payload) if attempt is not None else None
            if match is None or match.kind != "zero_match":
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
                resolution_evidence=deterministic_resolution_evidence(
                    reconcile_event_seq=intent.reconcile_event_seq,
                    payload=payload,
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
            payload = await fresh_reconcile(
                session, scope, row,
                reconcile_event_seq=intent.reconcile_event_seq, require_history=False,
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
                resolution_evidence=deterministic_resolution_evidence(
                    reconcile_event_seq=intent.reconcile_event_seq,
                    payload=payload,
                ),
            )
    except (ValueError, TypeError) as exc:
        # Event invariants (e.g. evidence shape) rejected the constructed event.
        raise ResolutionRejected("resolution_event_invalid") from exc
    raise ResolutionRejected("resolution_action_not_supported")


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

    def __init__(self, scope: ResolutionScope) -> None:
        self.scope = scope

    async def lock(self, session: AsyncSession) -> None:
        await acquire_transaction_lock(
            session,
            account_id=str(self.scope.account_id),
            deployment_environment=self.scope.environment,
        )

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
        await build_resolution_event(session, self.scope, intent, occurred_at_ms=now_ms)
        try:
            async with session.begin_nested():
                # Explicit column INSERT: an ORM flush would send the worker-only
                # columns as NULL and trip the column-scoped webapi grant.
                return (
                    await session.scalars(
                        insert(UncertaintyResolutionRequestRow)
                        .values(**request_values(self.scope, intent, now_ms=now_ms))
                        .returning(UncertaintyResolutionRequestRow)
                    )
                ).one()
        except IntegrityError:
            # A concurrent request won the pending slot: the same one is this
            # request, a different one is a conflict.
            pending = await self._pending_or_conflict(session, intent)
            if pending is None:
                raise
            return pending


def request_values(
    scope: ResolutionScope, intent: ResolutionIntent, *, now_ms: int
) -> dict[str, object]:
    """Exactly the web API's granted columns (``UNCERTAINTY_REQUEST_COLUMNS``)."""
    return {
        "request_id": uuid4(),
        "exchange_account_id": scope.account_id,
        "deployment_environment": scope.environment,
        "uncertainty_id": intent.uncertainty_id,
        "action": intent.action,
        "reconcile_event_seq": intent.reconcile_event_seq,
        "venue_offer_id": intent.venue_offer_id,
        "decision": intent.decision,
        "reason": intent.reason,
        "requested_by": intent.operator_id,
        "created_at_ms": now_ms,
    }


def _root_cause_name(exc: BaseException) -> str:
    seen: set[int] = set()
    current: BaseException = exc
    while current.__cause__ is not None and id(current.__cause__) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return type(current).__name__


class UncertaintyResolutionWorker:
    """Applies queued adjudications inside the account's single writer."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        scope: ResolutionScope,
        authority: OperatorAuthority,
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.session_factory = session_factory
        self.scope = scope
        # Required, not defaulted: an adjudication worker that forgot to check
        # who asked would apply a revoked operator's request.
        self.authority = authority
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.ownership = ownership
        self.poll_interval_s = poll_interval_s
        self.requests = UncertaintyResolutionRequests(scope)
        # Requests this process could neither apply nor mark failed. Skipped so
        # one bad row cannot starve the queue; a restart retries them.
        self._stuck: set[UUID] = set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = False
            try:
                processed = await self.tick()
            except Exception:
                # A failed tick leaves the request queued (its transaction rolled
                # back) and must not take the writer down with it.
                log.exception("uncertainty_resolution_tick_failed")
            if processed:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)

    async def tick(self) -> bool:
        """Settle at most one queued request. Returns whether one was taken.

        Every request taken leaves ``requested``: applied, rejected, failed, or
        -- if even the failure cannot be written -- skipped by this process.
        Otherwise a request whose outcome cannot be written would roll back,
        be picked again as the oldest, and block every request behind it.
        """
        if self.ownership is not None and not await self.ownership():
            raise RuntimeError("uncertainty_resolution_writer_ownership_lost")
        request_id: UUID | None = None
        try:
            async with self.session_factory.begin() as session:
                await self.requests.lock(session)
                query = (
                    select(UncertaintyResolutionRequestRow)
                    .where(
                        UncertaintyResolutionRequestRow.exchange_account_id
                        == self.scope.account_id,
                        UncertaintyResolutionRequestRow.deployment_environment
                        == self.scope.environment,
                        UncertaintyResolutionRequestRow.state == "requested",
                    )
                    .order_by(
                        UncertaintyResolutionRequestRow.created_at_ms,
                        UncertaintyResolutionRequestRow.request_id,
                    )
                    .limit(1)
                    .execution_options(populate_existing=True)
                )
                if self._stuck:
                    query = query.where(
                        UncertaintyResolutionRequestRow.request_id.not_in(self._stuck)
                    )
                request = await session.scalar(query)
                if request is None:
                    return False
                request_id = request.request_id
                await self._apply(session, request)
            return True
        except Exception as exc:
            if request_id is None:
                raise
            reason = "outcome_write_failed:" + _root_cause_name(exc)
            log.exception(
                "uncertainty_resolution_outcome_unwritten request_id=%s reason=%s",
                request_id, reason,
            )
            try:
                await self._mark_failed(request_id, reason)
            except Exception:
                log.exception("uncertainty_resolution_request_stuck request_id=%s", request_id)
                self._stuck.add(request_id)
            return True

    async def _mark_failed(self, request_id: UUID, reason: str) -> None:
        """Record a failure in its own transaction, after the apply rolled back.

        Guarded on ``requested``: if the apply's commit did land before the
        error surfaced, its recorded outcome stands.
        """
        async with self.session_factory.begin() as session:
            await self.requests.lock(session)
            await session.execute(
                update(UncertaintyResolutionRequestRow)
                .where(
                    UncertaintyResolutionRequestRow.request_id == request_id,
                    UncertaintyResolutionRequestRow.exchange_account_id == self.scope.account_id,
                    UncertaintyResolutionRequestRow.deployment_environment
                    == self.scope.environment,
                    UncertaintyResolutionRequestRow.state == "requested",
                )
                .values(state="failed", processed_at_ms=self.clock(), outcome_reason=reason)
            )

    async def _apply(self, session: AsyncSession, request: UncertaintyResolutionRequestRow) -> None:
        now = self.clock()
        state, reason, event_seq = "failed", None, None
        try:
            # Savepoint: a rejected or failed append leaves nothing behind but
            # the outcome recorded on the request.
            async with session.begin_nested():
                # The request was accepted while its operator was authorized;
                # applying it needs them to still be, in this transaction.
                if not await self.authority(
                    session, account_id=self.scope.account_id, user=request.requested_by
                ):
                    raise ResolutionRejected("operator_revoked")
                event = await build_resolution_event(
                    session, self.scope, ResolutionIntent.from_request(request), occurred_at_ms=now
                )
                result = await AccountEventWriter(
                    store=PostgresEventStore(deployment_environment=self.scope.environment)
                ).append(session, event)
            state, event_seq = "applied", result.event_seq
        except ResolutionRejected as exc:
            state, reason = "rejected", exc.code
        except ProjectionWriteError as exc:
            reason = "projection_write_failed:" + _root_cause_name(exc)
            log.exception(
                "uncertainty_resolution_failed request_id=%s reason=%s", request.request_id, reason
            )
        except Exception as exc:
            reason = "resolution_failed:" + _root_cause_name(exc)
            log.exception(
                "uncertainty_resolution_failed request_id=%s reason=%s", request.request_id, reason
            )
        request.state = state
        request.processed_at_ms = now
        request.resolved_event_seq = event_seq
        request.outcome_reason = reason
        await session.flush()
        log.info(
            "uncertainty_resolution_processed request_id=%s action=%s state=%s reason=%s",
            request.request_id, request.action, state, reason,
        )


__all__ = [
    "RECONCILE_EVENT_TYPE",
    "OperatorAuthority",
    "ResolutionAction",
    "ResolutionIntent",
    "ResolutionRejected",
    "ResolutionRequestPending",
    "ResolutionScope",
    "UncertaintyResolutionRequests",
    "UncertaintyResolutionWorker",
    "build_resolution_event",
    "fresh_reconcile",
    "load_attempt",
    "load_open_uncertainty",
    "load_scoped_uncertainty",
    "request_values",
]
