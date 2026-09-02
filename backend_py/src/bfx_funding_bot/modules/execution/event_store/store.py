from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.event_store.entities import (
    is_terminal_credit_status,
    is_terminal_offer_status,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_stored_event,
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    ProjectionHeadRow,
    ReconcileObservationRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    __SCHEMA_VERSION__,
    DEFAULT_RECONCILE_SYMBOL,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitOutcomeKind,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

_SUBMIT_UNCERTAINTY_NAMESPACE = UUID("d158ef54-c1dd-54e4-a9e9-9a670c938f73")


@dataclass(frozen=True, slots=True)
class SnapshotDrift:
    reserved_drift: Decimal
    realized_drift: Decimal
    event_seq: int | None = None


@dataclass(frozen=True, slots=True)
class StoreAppendResult:
    """Internal append outcome returned to the account writer."""

    persisted: bool
    event_seq: int


class OfferClaimIdentityConflictError(RuntimeError):
    """A CID projection attempted to change an established reservation identity."""


# Event types whose re-delivery must be deduped (idempotent fills/releases and
# one quarantine breadcrumb per venue object).
# NOTE: dedup for events with venue_seq IS NULL is app-level only — the
# uq_event_log_dedup unique index does not constrain NULLs (PG treats them as
# distinct). WS-sourced fills/releases carry venue_seq, so this gap is narrow.
# RESERVATION_CLAIMED is intentionally excluded: orphan-claim idempotency is
# enforced at the reconciliation compute layer (boot_recovery.compute_recovery_actions
# skips vois already in CLAIMED state), not by this store-level dedup.
_DEDUP_TYPES = frozenset({
    "ORDER_FILL",
    "RESERVATION_RELEASED",
    "VENUE_OFFER_QUARANTINED",
})

# Audit/attribution-only events: appended to event_log but NEVER projected onto
# position_state (live or rebuild tail) — realized/reserved stay reconcile-owned
# (single-writer invariant, ADR 2026-05-29).
_AUDIT_ONLY_TYPES = frozenset({"CREDIT_CLOSED"})

# offer_claims FSM state by event_type — cid-keyed projection. The voi-keyed
# transition() (registry_offers.py) is reserved for the in-memory OfferRegistry's
# fill-tracking; this snapshot is keyed by cid (stable across the whole lifecycle).
_CLAIM_STATE_BY_TYPE: dict[str, RegistryState] = {
    "RESERVATION_INTENT": RegistryState.PENDING,
    "RESERVATION_CLAIMED": RegistryState.CLAIMED,
    "RESERVATION_FAILED": RegistryState.FAILED,
    "SUBMIT_OUTCOME_UNKNOWN": RegistryState.UNKNOWN,
    "ORDER_FILL": RegistryState.RELEASED,
    "RESERVATION_RELEASED": RegistryState.RELEASED,
}


class PostgresEventStore:
    """Append-only event log plus compatibility projection primitives.

    ``append`` is the public compatibility wrapper.  The serialized writer
    owns the transaction lock and projection cursor, then calls
    ``_append_unlocked`` for the actual event/legacy projection work.
    """

    def __init__(self, *, deployment_environment: str) -> None:
        self._env = deployment_environment

    @property
    def deployment_environment(self) -> str:
        """Environment dimension used by all projections in this store."""
        return self._env

    async def append(self, session: AsyncSession, event: object) -> bool:
        """Append through :class:`AccountEventWriter` without committing.

        ``strict_identity=False`` is a deliberately transitional compatibility
        path for the repository's legacy synthetic realms.  Production rows
        already have canonical UUID ownership after the Halt 1 migration; new
        callers should instantiate ``AccountEventWriter`` directly.
        """
        from bfx_funding_bot.modules.execution.event_store.writer import (
            AccountEventWriter,
        )

        writer = AccountEventWriter(
            store=self,
            strict_identity=False,
            allow_missing_account=True,
        )
        result = await writer.append(session, event)
        return result.persisted

    async def append_snapshot(
        self,
        session: AsyncSession,
        event: VenueSnapshotObserved,
    ) -> SnapshotDrift:
        """Append a full-account observation and return materialized drift.

        The snapshot itself is projected by :class:`AccountEventWriter`; this
        helper only samples the previous canonical buckets so reconcile health
        can report a bounded divergence metric without a second write path.
        """
        account_id = event.account_id
        prior_rows = (
            await session.execute(
                select(PositionStateRow).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=PositionStateRow.exchange_account_id,
                        legacy_account_column=PositionStateRow.account_id,
                    ),
                    PositionStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        prior_offered = {
            row.symbol: Decimal(str(row.offered_amount))
            for row in prior_rows
        }
        prior_lent = {
            row.symbol: Decimal(str(row.lent_amount))
            for row in prior_rows
        }
        from bfx_funding_bot.modules.execution.event_store.writer import (
            AccountEventWriter,
        )

        append_result = await AccountEventWriter(store=self).append(session, event)
        event_seq = append_result.event_seq
        observed_offered: dict[str, Decimal] = {}
        observed_lent: dict[str, Decimal] = {}
        if event.coverage.active_offers_complete:
            for offer in event.offers:
                if not is_terminal_offer_status(offer.status):
                    observed_offered[offer.symbol] = (
                        observed_offered.get(offer.symbol, Decimal("0"))
                        + offer.amount_remaining
                    )
        if event.coverage.active_credits_complete:
            for credit in event.credits:
                if not is_terminal_credit_status(credit.status):
                    observed_lent[credit.symbol] = (
                        observed_lent.get(credit.symbol, Decimal("0"))
                        + credit.amount
                    )
        offered_symbols = set(prior_offered) | set(observed_offered)
        lent_symbols = set(prior_lent) | set(observed_lent)
        return SnapshotDrift(
            reserved_drift=sum(
                (abs(observed_offered.get(symbol, Decimal("0")) - prior_offered.get(symbol, Decimal("0")))
                 for symbol in offered_symbols),
                Decimal("0"),
            ),
            realized_drift=sum(
                (abs(observed_lent.get(symbol, Decimal("0")) - prior_lent.get(symbol, Decimal("0")))
                 for symbol in lent_symbols),
                Decimal("0"),
            ),
            event_seq=event_seq,
        )

    async def _append_unlocked(
        self, session: AsyncSession, event: object
    ) -> StoreAppendResult:
        """Append/project one event after the account lock is held."""
        etype = event_type_of(event)
        # Cast to Any so attribute access works on the dynamically-typed event object.
        _ev: Any = cast(Any, event)
        account_id: str = str(_ev.account_id)
        exchange_account_id = account_id_uuid_or_none(account_id)
        venue_offer_id: str | None = getattr(_ev, "venue_offer_id", None)
        venue_seq: int | None = getattr(_ev, "venue_seq", None)
        cid: int | None = getattr(_ev, "cid", None)
        occurred_at_ms: int = _ev.occurred_at_ms or 0
        payload: dict[str, Any] = serialize_event(event)

        existing_seq = None
        event_id = getattr(_ev, "event_id", None)
        if event_id is not None:
            existing = await self._find_event_id_row(
                session, account_id, event_id
            )
            if existing is not None:
                if existing.event_type != etype or existing.payload != payload:
                    raise OfferClaimIdentityConflictError(
                        f"event identity conflict event_id={event_id}"
                    )
                return StoreAppendResult(persisted=False, event_seq=existing.event_seq)
        if etype in _DEDUP_TYPES:
            existing_seq = await self._find_logged_seq(
                session, account_id, etype, venue_offer_id, venue_seq
            )
        if existing_seq is not None:
            return StoreAppendResult(persisted=False, event_seq=existing_seq)

        row = EventLogRow(
            account_id=account_id,
            exchange_account_id=exchange_account_id,
            deployment_environment=self._env,
            event_type=etype,
            cid=cid,
            venue_offer_id=venue_offer_id,
            venue_seq=venue_seq,
            event_id=getattr(_ev, "event_id", None),
            schema_version=getattr(_ev, "schema_version", __SCHEMA_VERSION__),
            payload=payload,
            occurred_at_ms=occurred_at_ms,
        )
        session.add(row)
        await session.flush()  # assigns row.event_seq
        await self._project_event_unlocked(
            session,
            event,
            account_id=account_id,
            event_seq=row.event_seq,
        )
        return StoreAppendResult(persisted=True, event_seq=row.event_seq)

    async def _project_event_unlocked(
        self,
        session: AsyncSession,
        event: object,
        *,
        account_id: str,
        event_seq: int,
    ) -> None:
        """Apply one already-persisted event to the compatibility snapshots.

        The account writer uses the same primitive for the current append and
        for replaying rows that were durable before a projection transaction
        completed.  Keeping this operation independent from event insertion is
        what makes the event-log cursor a recoverable projection boundary.
        """
        etype = event_type_of(event)
        if isinstance(event, VenueSnapshotObserved):
            if event.environment != self._env:
                raise ValueError(
                    "venue snapshot environment does not match event-store environment"
                )
            await self._project_venue_snapshot(
                session,
                event,
                account_id=account_id,
                event_seq=event_seq,
            )
            return
        if etype in _AUDIT_ONLY_TYPES:
            return
        event_obj: Any = cast(Any, event)
        await self._project_submission_attempt(
            session,
            event,
            account_id=account_id,
            event_seq=event_seq,
        )
        projection_changed = await self._project_offer_claims(
            session, event, account_id, event_seq=event_seq
        )
        if projection_changed:
            await self._project_position_state(
                session,
                etype,
                account_id,
                getattr(event_obj, "amount", None),
                event_seq,
                event_obj.occurred_at_ms or 0,
                symbol=event_obj.symbol,
            )
        if etype == "SUBMIT_OUTCOME_UNKNOWN":
            await self._project_submit_uncertainty(
                session,
                event,
                account_id=account_id,
                event_seq=event_seq,
            )

    async def _project_submission_attempt(
        self,
        session: AsyncSession,
        event: object,
        *,
        account_id: str,
        event_seq: int,
    ) -> None:
        """Project one immutable attempt and its single typed outcome.

        The event row, attempt mutation, offer/position projection, and cursor
        are owned by the surrounding :class:`AccountEventWriter` transaction.
        A compatibility stream without a registered ExchangeAccount predates
        submission attempts and remains event-only.
        """
        etype = event_type_of(event)
        if etype not in {
            "RESERVATION_INTENT",
            "RESERVATION_CLAIMED",
            "RESERVATION_FAILED",
            "SUBMIT_OUTCOME_UNKNOWN",
        }:
            return
        canonical = account_id_uuid_or_none(account_id)
        if canonical is None:
            return
        registered = await session.scalar(
            select(ExchangeAccount.id).where(ExchangeAccount.id == canonical)
        )
        if registered is None:
            return

        event_obj: Any = cast(Any, event)
        if etype == "RESERVATION_INTENT":
            attempt = event_obj.submission_attempt
            if attempt is None:
                # Historical PENDING intents predate submission_attempts. They
                # remain recoverable and must converge to UNKNOWN, never retry.
                return
            if not isinstance(attempt, SubmissionAttemptPayload):
                raise TypeError("submission_attempt must be SubmissionAttemptPayload")
            decision_row = await session.get(
                ExecutionDecisionRow,
                attempt.execution_decision_id,
            )
            if decision_row is None:
                raise OfferClaimIdentityConflictError(
                    "submission attempt has no execution decision"
                )
            if (
                decision_row.exchange_account_id != canonical
                or account_id_uuid_or_none(decision_row.account_id) != canonical
                or decision_row.deployment_environment != self._env
                or decision_row.symbol != event_obj.symbol
                or decision_row.signal_correlation_id
                != str(event_obj.signal_correlation_id)
            ):
                raise OfferClaimIdentityConflictError(
                    "submission attempt execution decision scope conflicts"
                )
            storage = attempt.as_storage_dict()
            if storage["environment"] != self._env:
                raise OfferClaimIdentityConflictError(
                    "submission attempt environment conflicts with event store"
                )
            existing = await session.scalar(
                select(SubmissionAttemptRow).where(
                    SubmissionAttemptRow.execution_decision_id
                    == attempt.execution_decision_id
                )
            )
            if existing is not None:
                raise OfferClaimIdentityConflictError(
                    "execution decision already has a submission attempt"
                )
            session.add(
                SubmissionAttemptRow(
                    attempt_id=attempt.attempt_id,
                    execution_decision_id=attempt.execution_decision_id,
                    exchange_account_id=canonical,
                    deployment_environment=attempt.environment,
                    symbol=attempt.symbol,
                    cid=attempt.cid,
                    normalized_payload=storage["normalized_payload"],
                    payload_sha256=attempt.payload_fingerprint,
                    started_at_ms=attempt.started_at_ms,
                    completed_at_ms=None,
                    outcome_kind=None,
                    outcome_reason=None,
                    venue_offer_id=None,
                    last_event_seq=event_seq,
                )
            )
            await session.flush()
            return

        reference = event_obj.reservation_ref
        if reference is None:
            return
        attempt_row = await session.scalar(
            select(SubmissionAttemptRow).where(
                SubmissionAttemptRow.execution_decision_id
                == reference.execution_decision_id
            )
        )
        if attempt_row is None:
            # Existing event-writer consumers may append venue lifecycle facts
            # which did not originate from the new command boundary.  They do
            # not manufacture an attempt retroactively; the command gate is the
            # only producer required to have one.
            return
        if (
            attempt_row.exchange_account_id != canonical
            or attempt_row.deployment_environment != self._env
            or attempt_row.symbol != event_obj.symbol
            or attempt_row.cid != event_obj.cid
        ):
            raise OfferClaimIdentityConflictError("submit outcome attempt scope conflicts")
        if attempt_row.outcome_kind is not None:
            raise OfferClaimIdentityConflictError(
                "submission attempt already has a typed outcome"
            )

        if etype == "RESERVATION_CLAIMED":
            outcome_kind = SubmitOutcomeKind.ACKNOWLEDGED
            outcome_reason = None
            venue_offer_id = event_obj.venue_offer_id
        elif etype == "SUBMIT_OUTCOME_UNKNOWN":
            outcome_kind = SubmitOutcomeKind.UNKNOWN
            outcome_reason = event_obj.reason
            venue_offer_id = None
        else:
            outcome_reason = event_obj.reason
            outcome_kind = (
                SubmitOutcomeKind.NOT_SENT
                if outcome_reason == "local_pre_transport"
                else SubmitOutcomeKind.REJECTED
            )
            venue_offer_id = None

        attempt_row.completed_at_ms = event_obj.occurred_at_ms or 0
        attempt_row.outcome_kind = outcome_kind.value
        attempt_row.outcome_reason = outcome_reason
        attempt_row.venue_offer_id = venue_offer_id
        attempt_row.last_event_seq = event_seq
        await session.flush()

    async def _project_submit_uncertainty(
        self,
        session: AsyncSession,
        event: object,
        *,
        account_id: str,
        event_seq: int,
    ) -> None:
        """Open the UNKNOWN block atomically with its outcome event."""
        canonical = account_id_uuid_or_none(account_id)
        if canonical is None:
            return
        registered = await session.scalar(
            select(ExchangeAccount.id).where(ExchangeAccount.id == canonical)
        )
        if registered is None:
            return
        event_obj: Any = cast(Any, event)
        reference = event_obj.reservation_ref
        if reference is None:
            raise OfferClaimIdentityConflictError(
                "submit uncertainty requires a reservation reference"
            )
        attempt = await session.scalar(
            select(SubmissionAttemptRow).where(
                SubmissionAttemptRow.execution_decision_id
                == reference.execution_decision_id
            )
        )
        if (
            attempt is not None
            and attempt.outcome_kind != SubmitOutcomeKind.UNKNOWN.value
        ):
            raise OfferClaimIdentityConflictError(
                "submit uncertainty requires an UNKNOWN submission attempt"
            )
        existing = await session.scalar(
            select(ExecutionUncertaintyRow).where(
                ExecutionUncertaintyRow.exchange_account_id == canonical,
                ExecutionUncertaintyRow.deployment_environment == self._env,
                ExecutionUncertaintyRow.symbol == event_obj.symbol,
                ExecutionUncertaintyRow.state == "open",
            )
        )
        if existing is not None:
            raise OfferClaimIdentityConflictError(
                "an open execution uncertainty already exists for this scope"
            )
        reason = str(event_obj.reason)[:256]
        if attempt is None:
            correlation_key = f"legacy_submit_unknown:{event_obj.event_id}"
        else:
            correlation_key = f"submission_attempt:{attempt.attempt_id}"
        uncertainty_id = uuid5(
            _SUBMIT_UNCERTAINTY_NAMESPACE,
            str(event_obj.event_id),
        )
        session.add(
            ExecutionUncertaintyRow(
                uncertainty_id=uncertainty_id,
                exchange_account_id=canonical,
                deployment_environment=self._env,
                symbol=event_obj.symbol,
                kind="submit_outcome_unknown",
                correlation_key=correlation_key,
                intended_amount=Decimal(str(event_obj.amount)),
                evidence={
                    "outcome_reason": reason,
                    "payload_sha256": (
                        attempt.payload_sha256 if attempt is not None else None
                    ),
                },
                attempt_id=attempt.attempt_id if attempt is not None else None,
                venue_offer_id=None,
                opened_event_seq=event_seq,
            )
        )
        await session.flush()

    async def _project_venue_snapshot(
        self,
        session: AsyncSession,
        event: VenueSnapshotObserved,
        *,
        account_id: str,
        event_seq: int,
    ) -> None:
        """Project one complete venue observation under the account writer lock."""
        exchange_account_id = account_id_uuid_or_none(account_id)
        if exchange_account_id is None:
            raise ValueError("venue snapshot projection requires canonical account UUID")

        prior_positions = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.exchange_account_id == exchange_account_id,
                    PositionStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        latest_snapshot_at = max(
            (row.last_venue_snapshot_at or 0 for row in prior_positions),
            default=0,
        )
        if latest_snapshot_at > event.query_finished_at_ms:
            # A delayed REST response may arrive after a fresher observation
            # committed.  Keep the event for audit, but never move canonical
            # exposure or object projections backwards in venue time.
            return

        existing_offer_rows = (
            await session.execute(
                select(VenueOfferStateRow).where(
                    VenueOfferStateRow.exchange_account_id == exchange_account_id,
                    VenueOfferStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        existing_credit_rows = (
            await session.execute(
                select(VenueCreditStateRow).where(
                    VenueCreditStateRow.exchange_account_id == exchange_account_id,
                    VenueCreditStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        observed_offer_ids = {offer.venue_offer_id for offer in event.offers}
        observed_credit_ids = {credit.credit_id for credit in event.credits}
        if event.coverage.active_offers_complete:
            for existing_offer in existing_offer_rows:
                if not existing_offer.is_terminal and existing_offer.venue_offer_id not in observed_offer_ids:
                    existing_offer.status = "absent"
                    existing_offer.is_terminal = True
                    existing_offer.last_seen_event_seq = max(
                        existing_offer.last_seen_event_seq, event_seq
                    )
        if event.coverage.active_credits_complete:
            for existing_credit in existing_credit_rows:
                if not existing_credit.is_terminal and existing_credit.credit_id not in observed_credit_ids:
                    existing_credit.status = "absent"
                    existing_credit.is_terminal = True
                    existing_credit.last_seen_event_seq = max(
                        existing_credit.last_seen_event_seq, event_seq
                    )

        # Entity projections are keyed by venue identity.  A terminal venue
        # object can never be reopened by a later active observation, and a
        # stale venue timestamp cannot overwrite a newer object state.
        for offer_observation in event.offers:
            offer_state_row = (
                await session.execute(
                    select(VenueOfferStateRow).where(
                        VenueOfferStateRow.exchange_account_id == exchange_account_id,
                        VenueOfferStateRow.deployment_environment == self._env,
                        VenueOfferStateRow.venue_offer_id == offer_observation.venue_offer_id,
                    )
                )
            ).scalar_one_or_none()
            terminal = is_terminal_offer_status(offer_observation.status)
            if offer_state_row is not None:
                if offer_state_row.is_terminal and not terminal:
                    continue
                if (
                    offer_observation.mts_updated < offer_state_row.mts_updated
                    or event_seq < offer_state_row.last_seen_event_seq
                ):
                    continue
                offer_state_row.symbol = offer_observation.symbol
                offer_state_row.amount_original = offer_observation.amount_original
                offer_state_row.amount_remaining = offer_observation.amount_remaining
                offer_state_row.rate = offer_observation.rate
                offer_state_row.period_days = offer_observation.period_days
                offer_state_row.status = offer_observation.status
                offer_state_row.flags = dict(offer_observation.flags)
                offer_state_row.mts_created = offer_observation.mts_created
                offer_state_row.mts_updated = offer_observation.mts_updated
                if offer_observation.cid is not None:
                    offer_state_row.cid = offer_observation.cid
                if offer_observation.execution_decision_id is not None:
                    offer_state_row.execution_decision_id = offer_observation.execution_decision_id
                if offer_observation.signal_correlation_id is not None:
                    offer_state_row.signal_correlation_id = str(offer_observation.signal_correlation_id)
                offer_state_row.last_seen_event_seq = max(offer_state_row.last_seen_event_seq, event_seq)
                offer_state_row.is_terminal = bool(offer_state_row.is_terminal or terminal)
                continue
            session.add(VenueOfferStateRow(
                exchange_account_id=exchange_account_id,
                deployment_environment=self._env,
                venue_offer_id=offer_observation.venue_offer_id,
                symbol=offer_observation.symbol,
                amount_original=offer_observation.amount_original,
                amount_remaining=offer_observation.amount_remaining,
                rate=offer_observation.rate,
                period_days=offer_observation.period_days,
                status=offer_observation.status,
                flags=dict(offer_observation.flags),
                mts_created=offer_observation.mts_created,
                mts_updated=offer_observation.mts_updated,
                cid=offer_observation.cid,
                execution_decision_id=offer_observation.execution_decision_id,
                signal_correlation_id=(
                    str(offer_observation.signal_correlation_id)
                    if offer_observation.signal_correlation_id is not None else None
                ),
                first_seen_event_seq=event_seq,
                last_seen_event_seq=event_seq,
                is_terminal=terminal,
            ))

        for credit_observation in event.credits:
            credit_state_row = (
                await session.execute(
                    select(VenueCreditStateRow).where(
                        VenueCreditStateRow.exchange_account_id == exchange_account_id,
                        VenueCreditStateRow.deployment_environment == self._env,
                        VenueCreditStateRow.credit_id == credit_observation.credit_id,
                    )
                )
            ).scalar_one_or_none()
            terminal = is_terminal_credit_status(credit_observation.status)
            incoming_updated = credit_observation.mts_updated or credit_observation.mts_created or 0
            existing_updated = (
                credit_state_row.mts_updated or credit_state_row.mts_created or 0
                if credit_state_row is not None else 0
            )
            if credit_state_row is not None:
                if credit_state_row.is_terminal and not terminal:
                    continue
                if incoming_updated < existing_updated or event_seq < credit_state_row.last_seen_event_seq:
                    continue
                credit_state_row.symbol = credit_observation.symbol
                credit_state_row.amount = credit_observation.amount
                credit_state_row.rate = credit_observation.rate
                credit_state_row.period_days = credit_observation.period_days
                credit_state_row.status = credit_observation.status
                credit_state_row.flags = dict(credit_observation.flags)
                credit_state_row.mts_created = credit_observation.mts_created
                credit_state_row.mts_updated = credit_observation.mts_updated
                credit_state_row.last_seen_event_seq = max(credit_state_row.last_seen_event_seq, event_seq)
                credit_state_row.is_terminal = bool(credit_state_row.is_terminal or terminal)
                continue
            session.add(VenueCreditStateRow(
                exchange_account_id=exchange_account_id,
                deployment_environment=self._env,
                credit_id=credit_observation.credit_id,
                symbol=credit_observation.symbol,
                amount=credit_observation.amount,
                rate=credit_observation.rate,
                period_days=credit_observation.period_days,
                status=credit_observation.status,
                flags=dict(credit_observation.flags),
                mts_created=credit_observation.mts_created,
                mts_updated=credit_observation.mts_updated,
                first_seen_event_seq=event_seq,
                last_seen_event_seq=event_seq,
                is_terminal=terminal,
            ))

        # Aggregate from the normalized entity rows, not directly from the
        # incoming payload.  This preserves object-level monotonicity when a
        # newer account snapshot carries an older venue timestamp for one item.
        projected_offer_rows = (
            await session.execute(
                select(VenueOfferStateRow).where(
                    VenueOfferStateRow.exchange_account_id == exchange_account_id,
                    VenueOfferStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        offers_by_symbol: dict[str, Decimal] = {}
        offer_counts: dict[str, int] = {}
        for offer_row in projected_offer_rows:
            if offer_row.is_terminal:
                continue
            offers_by_symbol[offer_row.symbol] = (
                offers_by_symbol.get(offer_row.symbol, Decimal("0"))
                + Decimal(str(offer_row.amount_remaining))
            )
            offer_counts[offer_row.symbol] = offer_counts.get(offer_row.symbol, 0) + 1
        projected_credit_rows = (
            await session.execute(
                select(VenueCreditStateRow).where(
                    VenueCreditStateRow.exchange_account_id == exchange_account_id,
                    VenueCreditStateRow.deployment_environment == self._env,
                )
            )
        ).scalars().all()
        credits_by_symbol: dict[str, Decimal] = {}
        credit_counts: dict[str, int] = {}
        for credit_row in projected_credit_rows:
            if credit_row.is_terminal:
                continue
            credits_by_symbol[credit_row.symbol] = (
                credits_by_symbol.get(credit_row.symbol, Decimal("0"))
                + Decimal(str(credit_row.amount))
            )
            credit_counts[credit_row.symbol] = credit_counts.get(credit_row.symbol, 0) + 1

        existing_positions = prior_positions
        prior_offered = {
            row.symbol: Decimal(str(row.offered_amount)) for row in existing_positions
        }
        prior_lent = {
            row.symbol: Decimal(str(row.lent_amount)) for row in existing_positions
        }
        prior_available = {
            row.symbol: Decimal(str(row.available_amount)) for row in existing_positions
        }
        prior_offer_counts: dict[str, int] = {}
        for existing_offer in existing_offer_rows:
            if not existing_offer.is_terminal:
                prior_offer_counts[existing_offer.symbol] = prior_offer_counts.get(existing_offer.symbol, 0) + 1
        prior_credit_counts: dict[str, int] = {}
        for existing_credit in existing_credit_rows:
            if not existing_credit.is_terminal:
                prior_credit_counts[existing_credit.symbol] = prior_credit_counts.get(existing_credit.symbol, 0) + 1
        if not event.coverage.active_offers_complete:
            offers_by_symbol = prior_offered
            offer_counts = prior_offer_counts
        if not event.coverage.active_credits_complete:
            credits_by_symbol = prior_lent
            credit_counts = prior_credit_counts
        symbols = (
            {row.symbol for row in existing_positions}
            | set(offers_by_symbol)
            | set(credits_by_symbol)
            | (set(event.wallet_available) if event.coverage.wallets_complete else set())
        )
        positions = {row.symbol: row for row in existing_positions}
        complete_authority = (
            event.coverage.active_offers_complete
            and event.coverage.active_credits_complete
            and event.coverage.wallets_complete
        )
        for symbol in sorted(symbols):
            offered = offers_by_symbol.get(symbol, Decimal("0"))
            lent = credits_by_symbol.get(symbol, Decimal("0"))
            available = (
                event.wallet_available.get(symbol, Decimal("0"))
                if event.coverage.wallets_complete
                else prior_available.get(symbol, Decimal("0"))
            )
            position_row = positions.get(symbol)
            if position_row is None:
                position_row = PositionStateRow(
                    account_id=account_id,
                    exchange_account_id=exchange_account_id,
                    deployment_environment=self._env,
                    symbol=symbol,
                    offered_amount=offered,
                    lent_amount=lent,
                    available_amount=available,
                    uncertain_amount=Decimal("0"),
                    reserved=offered,
                    realized=lent,
                    last_updated_ms=event.occurred_at_ms or event.query_finished_at_ms,
                    last_event_seq=event_seq,
                    last_venue_snapshot_at=(
                        event.query_finished_at_ms if complete_authority else None
                    ),
                    last_reconciled_at=(
                        event.query_finished_at_ms if complete_authority else None
                    ),
                    n_credits=(
                        credit_counts.get(symbol, 0)
                        if event.coverage.active_credits_complete else None
                    ),
                )
                session.add(position_row)
            else:
                assert position_row is not None
                position_row.offered_amount = offered
                position_row.lent_amount = lent
                position_row.available_amount = available
                position_row.reserved = offered
                position_row.realized = lent
                position_row.last_updated_ms = event.occurred_at_ms or event.query_finished_at_ms
                position_row.last_event_seq = max(position_row.last_event_seq, event_seq)
                if complete_authority:
                    position_row.last_venue_snapshot_at = event.query_finished_at_ms
                    position_row.last_reconciled_at = event.query_finished_at_ms
                if event.coverage.active_credits_complete:
                    position_row.n_credits = credit_counts.get(symbol, 0)
            session.add(ReconcileObservationRow(
                account_id=account_id,
                exchange_account_id=exchange_account_id,
                deployment_environment=self._env,
                symbol=symbol,
                reserved_usdt=offered,
                realized_usdt=lent,
                n_offers=offer_counts.get(symbol, 0),
                n_credits=credit_counts.get(symbol, 0),
                observed_at_ms=event.query_finished_at_ms,
                event_seq_fence=event_seq,
            ))

    async def _find_event_id_row(
        self, session: AsyncSession, account_id: str, event_id: object
    ) -> EventLogRow | None:
        stmt = select(EventLogRow).where(
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=EventLogRow.exchange_account_id,
                legacy_account_column=EventLogRow.account_id,
            ),
            EventLogRow.deployment_environment == self._env,
            EventLogRow.event_id == event_id,
        ).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _find_logged_seq(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> int | None:
        stmt = (
            select(EventLogRow.event_seq)
            .where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=EventLogRow.exchange_account_id,
                    legacy_account_column=EventLogRow.account_id,
                ),
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type == event_type,
                EventLogRow.venue_offer_id == venue_offer_id,
                EventLogRow.venue_seq == venue_seq,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _already_logged(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> bool:
        return (
            await self._find_logged_seq(
                session, account_id, event_type, venue_offer_id, venue_seq
            )
        ) is not None

    async def _project_offer_claims(
        self,
        session: AsyncSession,
        event: object,
        account_id: str,
        *,
        event_seq: int | None = None,
    ) -> bool:
        """Project event onto the cid-keyed offer_claims snapshot (same txn).

        Direct event_type -> state mapping; no pre-select, no voi-keyed
        transition(). cid is stable across the whole lifecycle so a single
        row is upserted in place (PENDING -> CLAIMED -> RELEASED/FAILED).
        """
        etype = event_type_of(event)
        state = _CLAIM_STATE_BY_TYPE.get(etype)
        if state is None:
            return False  # audit-only events (e.g. cancel) are not claim-bearing
        _ev: Any = cast(Any, event)
        cid: int | None = getattr(_ev, "cid", None)
        if cid is None:
            return False  # no cid -> nothing to key on
        now_ms: int = _ev.occurred_at_ms or 0
        # All claim-bearing events now carry canonical native `amount` (mirrored
        # from size_usdt by events._resolve_amount); use it directly.
        symbol = getattr(_ev, "symbol", None)
        if symbol is None:
            raise ValueError(f"{etype} reached offer_claims projection without symbol")
        return await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            symbol=symbol,
            size_usdt=Decimal(str(_ev.amount)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            execution_decision_id=(
                _ev.reservation_ref.execution_decision_id
                if getattr(_ev, "reservation_ref", None) is not None
                else None
            ),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
            last_event_seq=event_seq or 0,
        )

    async def _upsert_claim(
        self,
        session: AsyncSession,
        *,
        cid: int,
        account_id: str,
        state: RegistryState,
        venue_offer_id: str | None,
        symbol: str,
        size_usdt: Decimal,
        signal_correlation_id: str,
        execution_decision_id: str | None,
        occurred_at_ms: int,
        last_updated_ms: int,
        last_event_seq: int = 0,
    ) -> bool:
        """Atomically insert then compare every available reservation identity.

        The database owns concurrent first-writer arbitration.  A losing writer
        re-reads the canonical row by CID, venue offer id, or audited decision
        id and may only continue for an exact identity match.
        """
        values: dict[str, Any] = {
            "cid": cid,
            "account_id": account_id,
            "exchange_account_id": account_id_uuid_or_none(account_id),
            "deployment_environment": self._env,
            "symbol": symbol,
            "state": state.value,
            "venue_offer_id": venue_offer_id,
            "size_usdt": size_usdt,
            "signal_correlation_id": signal_correlation_id,
            "execution_decision_id": execution_decision_id,
            "occurred_at_ms": occurred_at_ms,
            "last_updated_ms": last_updated_ms,
            "last_event_seq": last_event_seq,
        }
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        insert_result: Any = await session.execute(
            insert(OfferClaimRow).values(values).on_conflict_do_nothing(),
        )
        inserted = insert_result.rowcount == 1

        identity_terms = [OfferClaimRow.cid == cid]
        if venue_offer_id is not None:
            identity_terms.append(OfferClaimRow.venue_offer_id == venue_offer_id)
        if execution_decision_id is not None:
            identity_terms.append(OfferClaimRow.execution_decision_id == execution_decision_id)
        matches = (await session.execute(
            select(OfferClaimRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=OfferClaimRow.exchange_account_id,
                    legacy_account_column=OfferClaimRow.account_id,
                ),
                OfferClaimRow.deployment_environment == self._env,
                or_(*identity_terms),
            ),
        )).scalars().all()
        if len(matches) != 1:
            raise OfferClaimIdentityConflictError(
                f"claim identity conflict cid={cid}: canonical matches={len(matches)}",
            )
        existing = matches[0]
        if existing.signal_correlation_id != signal_correlation_id:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: signal")
        if existing.cid != cid:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: cid")
        if (
            existing.execution_decision_id is not None
            and existing.execution_decision_id != execution_decision_id
        ):
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: decision")
        if (
            existing.venue_offer_id is not None
            and existing.venue_offer_id != venue_offer_id
        ):
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: venue offer")
        if existing.symbol != symbol:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: symbol")
        if Decimal(str(existing.size_usdt)) != size_usdt:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: amount")

        changed = (
            inserted
            or existing.state != state.value
            or last_event_seq > existing.last_event_seq
        )
        next_execution_decision_id = existing.execution_decision_id
        if next_execution_decision_id is None and execution_decision_id is not None:
            next_execution_decision_id = execution_decision_id
            changed = True
        next_venue_offer_id = existing.venue_offer_id
        if next_venue_offer_id is None and venue_offer_id is not None:
            next_venue_offer_id = venue_offer_id
            changed = True
        if changed:
            if existing.exchange_account_id is None:
                # SQLite historical synthetic fixtures use a NULL UUID owner
                # while production rows are UUID-keyed.  Core UPDATE avoids
                # asking SQLAlchemy to flush a legacy object whose final ORM
                # primary key is intentionally incomplete.
                session.expunge(existing)
                await session.execute(
                    update(OfferClaimRow)
                    .where(
                        OfferClaimRow.account_id == account_id,
                        OfferClaimRow.deployment_environment == self._env,
                        OfferClaimRow.cid == cid,
                    )
                    .values(
                        state=state.value,
                        last_updated_ms=last_updated_ms,
                        execution_decision_id=next_execution_decision_id,
                        venue_offer_id=next_venue_offer_id,
                        last_event_seq=max(existing.last_event_seq, last_event_seq),
                    )
                )
            else:
                existing.execution_decision_id = next_execution_decision_id
                existing.venue_offer_id = next_venue_offer_id
                existing.state = state.value
                existing.last_updated_ms = last_updated_ms
                existing.last_event_seq = max(existing.last_event_seq, last_event_seq)
        return changed

    async def _project_position_state(
        self,
        session: AsyncSession,
        etype: str,
        account_id: str,
        size_usdt: Any,
        event_seq: int,
        occurred_at_ms: int,
        *,
        symbol: str,
    ) -> None:
        size = Decimal(str(size_usdt)) if size_usdt is not None else Decimal("0")
        ps = (
            await session.execute(
                select(PositionStateRow).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=PositionStateRow.exchange_account_id,
                        legacy_account_column=PositionStateRow.account_id,
                    ),
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        is_new = ps is None
        if is_new:
            ps = PositionStateRow(
                account_id=account_id,
                exchange_account_id=account_id_uuid_or_none(account_id),
                deployment_environment=self._env,
                symbol=symbol,
                reserved=Decimal("0"),
                realized=Decimal("0"),
                offered_amount=Decimal("0"),
                lent_amount=Decimal("0"),
                available_amount=Decimal("0"),
                uncertain_amount=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
            session.add(ps)
        assert ps is not None
        reserved = Decimal(str(ps.reserved))
        realized = Decimal(str(ps.realized))
        offered = Decimal(str(ps.offered_amount))
        lent = Decimal(str(ps.lent_amount))
        uncertain = Decimal(str(ps.uncertain_amount))
        if etype == "RESERVATION_CLAIMED":
            reserved += size
            offered += size
        elif etype == "ORDER_FILL":
            delta = min(reserved, size)
            reserved -= delta
            realized += size
            offered = max(Decimal("0"), offered - delta)
            lent += size
        elif etype == "RESERVATION_RELEASED":
            delta = min(reserved, size)
            reserved -= delta
            offered = max(Decimal("0"), offered - delta)
        elif etype == "SUBMIT_OUTCOME_UNKNOWN":
            uncertain += size
        if not is_new and ps.exchange_account_id is None:
            await session.execute(
                update(PositionStateRow)
                .where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
                .values(
                    reserved=reserved,
                    realized=realized,
                    offered_amount=offered,
                    lent_amount=lent,
                    uncertain_amount=uncertain,
                    last_updated_ms=occurred_at_ms,
                    last_event_seq=event_seq,
                )
            )
        else:
            ps.reserved = reserved
            ps.realized = realized
            ps.offered_amount = offered
            ps.lent_amount = lent
            ps.uncertain_amount = uncertain
            ps.last_updated_ms = occurred_at_ms
            ps.last_event_seq = event_seq

    async def rebuild_snapshot_from_log(
        self, session: AsyncSession, *, account_id: str, deployment_environment: str,
        symbol: str | None = None,
    ) -> None:
        """Rebuild snapshots for (account, env).

        A full rebuild (``symbol=None``) replays the immutable event stream from
        an empty projection.  This is the canonical path once
        ``VENUE_SNAPSHOT_OBSERVED`` events exist.  The symbol-specific argument
        remains as a compatibility path for pre-cutover reconcile checkpoints.

        offer_claims: folded from the full event_log (FSM, cheap).
        position_state: latest reconcile_observation checkpoint ⊕ domain events
        with event_seq > fence. Falls back to genesis fold if no checkpoint.
        """
        if symbol is None:
            await self._rebuild_full_from_log(
                session,
                account_id=account_id,
                deployment_environment=deployment_environment,
            )
            return
        await session.execute(delete(OfferClaimRow).where(
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=OfferClaimRow.exchange_account_id,
                legacy_account_column=OfferClaimRow.account_id,
            ),
            OfferClaimRow.deployment_environment == deployment_environment))
        await session.execute(delete(PositionStateRow).where(
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=PositionStateRow.exchange_account_id,
                legacy_account_column=PositionStateRow.account_id,
            ),
            PositionStateRow.deployment_environment == deployment_environment,
            PositionStateRow.symbol == symbol))
        await session.flush()

        rows = (await session.execute(
            select(EventLogRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=EventLogRow.exchange_account_id,
                    legacy_account_column=EventLogRow.account_id,
                ),
                EventLogRow.deployment_environment == deployment_environment,
            ).order_by(EventLogRow.event_seq.asc())
        )).scalars().all()

        # offer_claims: full fold.
        for r in rows:
            event = deserialize_stored_event(r)
            await self._project_offer_claims(
                session, event, account_id, event_seq=r.event_seq
            )

        # position_state: checkpoint + tail. The checkpoint base is now per-symbol
        # (reconcile_observation.symbol) so fUST/fUSD rebuild from their own base.
        checkpoint = (await session.execute(
            select(ReconcileObservationRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=ReconcileObservationRow.exchange_account_id,
                    legacy_account_column=ReconcileObservationRow.account_id,
                ),
                ReconcileObservationRow.deployment_environment == deployment_environment,
                ReconcileObservationRow.symbol == symbol,
            ).order_by(ReconcileObservationRow.id.desc()).limit(1)
        )).scalar_one_or_none()

        if checkpoint is None:
            fence = 0
            base_reserved = Decimal("0")
            base_realized = Decimal("0")
            base_seq = 0
            base_ms = 0
        else:
            fence = checkpoint.event_seq_fence
            base_reserved = Decimal(str(checkpoint.reserved_usdt))
            base_realized = Decimal(str(checkpoint.realized_usdt))
            base_seq = checkpoint.event_seq_fence
            base_ms = checkpoint.observed_at_ms

        ps = PositionStateRow(
            account_id=account_id,
            exchange_account_id=account_id_uuid_or_none(account_id),
            deployment_environment=deployment_environment,
            symbol=symbol,
            reserved=base_reserved,
            realized=base_realized,
            last_updated_ms=base_ms,
            last_event_seq=base_seq,
            last_reconciled_at=(checkpoint.observed_at_ms if checkpoint else None),
            n_credits=(checkpoint.n_credits if checkpoint else None),
        )
        session.add(ps)

        reserved = base_reserved
        realized = base_realized
        for r in rows:
            if r.event_seq <= fence:
                continue
            if r.event_type in _AUDIT_ONLY_TYPES:
                continue  # mirror the live-append skip: no ledger fold, no seq bump
            # Phase 2: fUSD/fUST coexist in one event_log, so the tail fold MUST
            # filter on payload["symbol"] == symbol — otherwise the other
            # currency's deltas mix into this symbol's position_state row.
            # Legacy rows predate the symbol column (no `symbol` key); they were
            # fUST-era, so default a missing symbol to DEFAULT_RECONCILE_SYMBOL —
            # otherwise this filter drops them and silently miscomputes realized
            # on a genesis rebuild (the deploy pre-flight foot-gun).
            if ((r.payload or {}).get("symbol") or DEFAULT_RECONCILE_SYMBOL) != symbol:
                continue
            # Raw payload read (no deserialize_event) — intentional: the tail fold
            # only needs the native `amount` and stays decoupled from domain event
            # objects. If a new event type gains a non-string-serialized amount,
            # sync this with serialization.py.
            size = Decimal(str((r.payload or {}).get("amount", 0) or 0))
            if r.event_type == "RESERVATION_CLAIMED":
                reserved += size
            elif r.event_type == "ORDER_FILL":
                delta = min(reserved, size)
                reserved -= delta
                realized += size
            elif r.event_type == "RESERVATION_RELEASED":
                reserved -= min(reserved, size)
            ps.reserved = reserved
            ps.realized = realized
            ps.last_updated_ms = r.occurred_at_ms
            ps.last_event_seq = r.event_seq

    async def _rebuild_full_from_log(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        deployment_environment: str,
    ) -> None:
        """Replay all event types into clean account-scoped projections."""
        scope_tables = (
            (
                ExecutionUncertaintyRow,
                ExecutionUncertaintyRow.exchange_account_id,
                None,
            ),
            (
                SubmissionAttemptRow,
                SubmissionAttemptRow.exchange_account_id,
                None,
            ),
            (OfferClaimRow, OfferClaimRow.exchange_account_id, OfferClaimRow.account_id),
            (PositionStateRow, PositionStateRow.exchange_account_id, PositionStateRow.account_id),
            (VenueOfferStateRow, VenueOfferStateRow.exchange_account_id, None),
            (VenueCreditStateRow, VenueCreditStateRow.exchange_account_id, None),
            (ReconcileObservationRow, ReconcileObservationRow.exchange_account_id, ReconcileObservationRow.account_id),
        )
        for table, exchange_account_column, legacy_account_column in scope_tables:
            await session.execute(
                delete(table).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=exchange_account_column,
                        legacy_account_column=legacy_account_column,
                    ),
                    table.deployment_environment == deployment_environment,
                )
            )
        await session.flush()
        rows = (
            await session.execute(
                select(EventLogRow).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
                    EventLogRow.deployment_environment == deployment_environment,
                ).order_by(EventLogRow.event_seq.asc())
            )
        ).scalars().all()
        for row in rows:
            event = deserialize_stored_event(row)
            await self._project_event_unlocked(
                session,
                event,
                account_id=account_id,
                event_seq=row.event_seq,
            )
        canonical = account_id_uuid_or_none(account_id)
        if canonical is not None:
            max_event_seq = rows[-1].event_seq if rows else 0
            head = (
                await session.execute(
                    select(ProjectionHeadRow).where(
                        ProjectionHeadRow.exchange_account_id == canonical,
                        ProjectionHeadRow.deployment_environment == deployment_environment,
                        ProjectionHeadRow.projection_name == "execution_state",
                    )
                )
            ).scalar_one_or_none()
            if head is None:
                session.add(ProjectionHeadRow(
                    exchange_account_id=canonical,
                    deployment_environment=deployment_environment,
                    projection_name="execution_state",
                    last_event_seq=max_event_seq,
                    projector_version="execution-state-v1",
                ))
            else:
                head.last_event_seq = max_event_seq
        await session.flush()
        if canonical is not None:
            verified_head = await session.scalar(
                select(ProjectionHeadRow.last_event_seq).where(
                    ProjectionHeadRow.exchange_account_id == canonical,
                    ProjectionHeadRow.deployment_environment == deployment_environment,
                    ProjectionHeadRow.projection_name == "execution_state",
                )
            )
            if int(verified_head or 0) != max_event_seq:
                raise RuntimeError(
                    "deterministic projection rebuild cursor mismatch: "
                    f"expected={max_event_seq}, actual={verified_head!r}"
                )
