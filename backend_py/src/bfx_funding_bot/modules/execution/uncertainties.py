"""Domain persistence boundary for submission attempts and uncertainties."""
from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

__all__ = ["UncertaintyKind", "UncertaintyService"]

_MAX_EVIDENCE_BYTES = 16_384
_RECONCILE_EVENT_TYPE = "VENUE_SNAPSHOT_OBSERVED"


class UncertaintyKind(StrEnum):
    """Closed uncertainty vocabulary. Unknown future values fail closed."""

    SUBMIT_OUTCOME_UNKNOWN = "submit_outcome_unknown"
    UNATTRIBUTED_VENUE_OFFER = "unattributed_venue_offer"
    UNSUPPORTED_VENUE_EXPOSURE = "unsupported_venue_exposure"


def _kind(value: UncertaintyKind | str) -> UncertaintyKind:
    try:
        return UncertaintyKind(value)
    except ValueError as exc:
        raise ValueError(f"unsupported uncertainty kind: {value!r}") from exc


def _nonempty(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


def _nonnegative(value: Decimal | int | float | str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount < 0:
        raise ValueError("intended_amount must be non-negative")
    return amount


def _bounded_evidence(value: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence exceeds {_MAX_EVIDENCE_BYTES} bytes")
    # The JSON round trip rejects non-JSON implementations and detaches caller
    # owned mutable data before it becomes an audit value.
    return cast(dict[str, Any], json.loads(encoded))


class UncertaintyService:
    """Write the durable evidence needed by later command/reconcile tasks.

    Task 2 owns idempotency and the safety projection only.  It deliberately
    performs no venue inference, command gating, API authorization, or event
    appending; those behaviours are sequenced by later task boundaries.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_attempt(self, payload: SubmissionAttemptPayload) -> SubmissionAttemptRow:
        """Persist one immutable payload identity, returning a replayed row."""
        storage = payload.as_storage_dict()
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(SubmissionAttemptRow).where(
                    SubmissionAttemptRow.execution_decision_id
                    == payload.execution_decision_id
                )
            )
            if existing is not None:
                self._require_same_attempt(existing, payload)
                return existing
            row = SubmissionAttemptRow(
                execution_decision_id=payload.execution_decision_id,
                exchange_account_id=payload.account_id,
                deployment_environment=payload.environment,
                symbol=payload.symbol,
                cid=payload.cid,
                normalized_payload=storage["normalized_payload"],
                payload_sha256=payload.payload_fingerprint,
                started_at_ms=payload.started_at_ms,
                completed_at_ms=payload.completed_at_ms,
                outcome_kind=storage["outcome_kind"],
                outcome_reason=payload.outcome_reason,
                venue_offer_id=payload.venue_offer_id,
                last_event_seq=payload.last_event_seq,
            )
            session.add(row)
            await session.commit()
            return row

    async def open_or_get(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
        kind: UncertaintyKind | str,
        correlation_key: str,
        intended_amount: Decimal | int | float | str,
        evidence: dict[str, Any],
        opened_event_seq: int,
        attempt_id: UUID | None = None,
        venue_offer_id: str | None = None,
    ) -> ExecutionUncertaintyRow:
        """Open a scoped block exactly once and pessimistically reserve it."""
        typed_kind = _kind(kind)
        environment = _nonempty(deployment_environment, field="deployment_environment")
        scoped_symbol = _nonempty(symbol, field="symbol")
        correlation = _nonempty(correlation_key, field="correlation_key")
        amount = _nonnegative(intended_amount)
        if opened_event_seq < 0:
            raise ValueError("opened_event_seq must be non-negative")
        stored_evidence = _bounded_evidence(evidence)
        linked_venue_offer_id = self._validate_venue_link(
            kind=typed_kind,
            venue_offer_id=venue_offer_id,
        )

        async with self._session_factory() as session:
            # PostgreSQL serializes all opens which share this safety projection,
            # including different uncertainty kinds. SQLite accepts FOR UPDATE as
            # a no-op, while the unique-conflict recovery below remains correct.
            position = await session.scalar(
                select(PositionStateRow)
                .where(
                    PositionStateRow.exchange_account_id == exchange_account_id,
                    PositionStateRow.deployment_environment == environment,
                    PositionStateRow.symbol == scoped_symbol,
                )
                .with_for_update()
            )
            if position is None:
                raise ValueError("position state is required before opening an uncertainty")

            existing = await session.scalar(
                select(ExecutionUncertaintyRow)
                .where(
                    ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                    ExecutionUncertaintyRow.deployment_environment == environment,
                    ExecutionUncertaintyRow.symbol == scoped_symbol,
                    ExecutionUncertaintyRow.kind == typed_kind.value,
                    ExecutionUncertaintyRow.correlation_key == correlation,
                )
                .with_for_update()
            )
            if existing is not None:
                self._require_same_uncertainty(
                    existing,
                    exchange_account_id=exchange_account_id,
                    deployment_environment=environment,
                    symbol=scoped_symbol,
                    kind=typed_kind,
                    correlation_key=correlation,
                    intended_amount=amount,
                    evidence=stored_evidence,
                    opened_event_seq=opened_event_seq,
                    attempt_id=attempt_id,
                    venue_offer_id=linked_venue_offer_id,
                )
                return existing

            scope_open = await session.scalar(
                select(ExecutionUncertaintyRow)
                .where(
                    ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                    ExecutionUncertaintyRow.deployment_environment == environment,
                    ExecutionUncertaintyRow.symbol == scoped_symbol,
                    ExecutionUncertaintyRow.kind == typed_kind.value,
                    ExecutionUncertaintyRow.state == "open",
                )
                .with_for_update()
            )
            if scope_open is not None:
                raise ValueError("an open uncertainty already exists for this exact scope")

            await self._require_event(
                session,
                event_seq=opened_event_seq,
                exchange_account_id=exchange_account_id,
                deployment_environment=environment,
            )
            if attempt_id is not None:
                await self._require_attempt_scope(
                    session,
                    attempt_id=attempt_id,
                    exchange_account_id=exchange_account_id,
                    deployment_environment=environment,
                    symbol=scoped_symbol,
                )
            if linked_venue_offer_id is not None:
                await self._require_venue_offer_scope(
                    session,
                    venue_offer_id=linked_venue_offer_id,
                    exchange_account_id=exchange_account_id,
                    deployment_environment=environment,
                    symbol=scoped_symbol,
                )
            reserved = await session.execute(
                update(PositionStateRow)
                .where(
                    PositionStateRow.exchange_account_id == exchange_account_id,
                    PositionStateRow.deployment_environment == environment,
                    PositionStateRow.symbol == scoped_symbol,
                )
                .values(
                    uncertain_amount=PositionStateRow.uncertain_amount + amount,
                    last_event_seq=case(
                        (PositionStateRow.last_event_seq < opened_event_seq, opened_event_seq),
                        else_=PositionStateRow.last_event_seq,
                    ),
                )
                .returning(PositionStateRow.exchange_account_id)
            )
            if reserved.scalar_one_or_none() is None:
                raise ValueError("position state is required before opening an uncertainty")

            row = ExecutionUncertaintyRow(
                exchange_account_id=exchange_account_id,
                deployment_environment=environment,
                symbol=scoped_symbol,
                kind=typed_kind.value,
                correlation_key=correlation,
                intended_amount=amount,
                evidence=stored_evidence,
                attempt_id=attempt_id,
                venue_offer_id=linked_venue_offer_id,
                opened_event_seq=opened_event_seq,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # A SQLite/no-lock race (or an independent PostgreSQL writer)
                # can win after our position lock. Roll back the atomic reserve,
                # then treat an exact correlation winner as an idempotent replay.
                await session.rollback()
                winner = await session.scalar(
                    select(ExecutionUncertaintyRow)
                    .where(
                        ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                        ExecutionUncertaintyRow.deployment_environment == environment,
                        ExecutionUncertaintyRow.symbol == scoped_symbol,
                        ExecutionUncertaintyRow.kind == typed_kind.value,
                        ExecutionUncertaintyRow.correlation_key == correlation,
                    )
                    .with_for_update()
                )
                if winner is None:
                    raise
                self._require_same_uncertainty(
                    winner,
                    exchange_account_id=exchange_account_id,
                    deployment_environment=environment,
                    symbol=scoped_symbol,
                    kind=typed_kind,
                    correlation_key=correlation,
                    intended_amount=amount,
                    evidence=stored_evidence,
                    opened_event_seq=opened_event_seq,
                    attempt_id=attempt_id,
                    venue_offer_id=linked_venue_offer_id,
                )
                return winner
            else:
                return row

    async def resolve(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
        kind: UncertaintyKind | str,
        reconcile_event_seq: int,
        resolution_event_seq: int,
        resolved_by_operator_id: str,
        resolution_reason: str,
        evidence: dict[str, Any],
    ) -> ExecutionUncertaintyRow:
        """Resolve one exact scope only after fresh, scoped reconcile evidence."""
        typed_kind = _kind(kind)
        environment = _nonempty(deployment_environment, field="deployment_environment")
        scoped_symbol = _nonempty(symbol, field="symbol")
        operator_id = _nonempty(resolved_by_operator_id, field="operator id")
        reason = _nonempty(resolution_reason, field="resolution_reason")
        if reconcile_event_seq < 0:
            raise ValueError("reconcile_event_seq must be non-negative")
        if resolution_event_seq < 0:
            raise ValueError("resolution_event_seq must be non-negative")
        resolution_evidence = _bounded_evidence(evidence)

        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutionUncertaintyRow)
                .where(
                    ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                    ExecutionUncertaintyRow.deployment_environment == environment,
                    ExecutionUncertaintyRow.symbol == scoped_symbol,
                    ExecutionUncertaintyRow.kind == typed_kind.value,
                    ExecutionUncertaintyRow.state == "open",
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("no open uncertainty exists for this exact scope")
            if reconcile_event_seq <= row.opened_event_seq:
                raise ValueError("fresh reconcile event is required for resolution")
            reconcile_event = await self._require_event(
                session,
                event_seq=reconcile_event_seq,
                exchange_account_id=exchange_account_id,
                deployment_environment=environment,
            )
            if reconcile_event.event_type != _RECONCILE_EVENT_TYPE:
                raise ValueError("fresh reconcile event is required for resolution")
            if resolution_event_seq <= reconcile_event_seq:
                raise ValueError("resolution event must follow fresh reconcile evidence")
            resolution_event = await self._require_event(
                session,
                event_seq=resolution_event_seq,
                exchange_account_id=exchange_account_id,
                deployment_environment=environment,
            )
            if resolution_event.event_type == _RECONCILE_EVENT_TYPE:
                raise ValueError("resolution event must not be a venue snapshot")
            released = await session.execute(
                update(PositionStateRow)
                .where(
                    PositionStateRow.exchange_account_id == exchange_account_id,
                    PositionStateRow.deployment_environment == environment,
                    PositionStateRow.symbol == scoped_symbol,
                    PositionStateRow.uncertain_amount >= row.intended_amount,
                )
                .values(
                    uncertain_amount=PositionStateRow.uncertain_amount - row.intended_amount,
                    last_event_seq=case(
                        (
                            PositionStateRow.last_event_seq < resolution_event_seq,
                            resolution_event_seq,
                        ),
                        else_=PositionStateRow.last_event_seq,
                    ),
                )
                .returning(PositionStateRow.exchange_account_id)
            )
            if released.scalar_one_or_none() is None:
                raise ValueError("uncertain projection cannot become negative")

            row.state = "resolved"
            row.reconcile_event_seq = reconcile_event_seq
            row.resolved_event_seq = resolution_event_seq
            row.resolved_by_operator_id = operator_id
            row.resolution_reason = reason
            row.resolution_evidence = resolution_evidence
            row.resolved_at = resolution_event.recorded_at
            await session.commit()
            return row

    @staticmethod
    def _require_same_attempt(
        row: SubmissionAttemptRow, payload: SubmissionAttemptPayload
    ) -> None:
        storage = payload.as_storage_dict()
        if (
            row.execution_decision_id != storage["execution_decision_id"]
            or str(row.exchange_account_id) != storage["account_id"]
            or row.deployment_environment != storage["environment"]
            or row.symbol != storage["symbol"]
            or row.cid != storage["cid"]
            or row.normalized_payload != storage["normalized_payload"]
            or row.payload_sha256 != storage["payload_sha256"]
            or row.started_at_ms != storage["started_at_ms"]
            or row.completed_at_ms != storage["completed_at_ms"]
            or row.outcome_kind != storage["outcome_kind"]
            or row.outcome_reason != storage["outcome_reason"]
            or row.venue_offer_id != storage["venue_offer_id"]
            or row.last_event_seq != storage["last_event_seq"]
        ):
            raise ValueError("execution decision already has a different immutable attempt")

    @staticmethod
    def _require_same_uncertainty(
        row: ExecutionUncertaintyRow,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
        kind: UncertaintyKind,
        correlation_key: str,
        intended_amount: Decimal,
        evidence: dict[str, Any],
        opened_event_seq: int,
        attempt_id: UUID | None,
        venue_offer_id: str | None,
    ) -> None:
        if (
            row.exchange_account_id != exchange_account_id
            or row.deployment_environment != deployment_environment
            or row.symbol != symbol
            or row.kind != kind.value
            or row.correlation_key != correlation_key
            or row.intended_amount != intended_amount
            or row.evidence != evidence
            or row.opened_event_seq != opened_event_seq
            or row.attempt_id != attempt_id
            or row.venue_offer_id != venue_offer_id
        ):
            raise ValueError("correlation key already has a different immutable uncertainty")

    @staticmethod
    def _validate_venue_link(
        *, kind: UncertaintyKind, venue_offer_id: str | None
    ) -> str | None:
        if kind is UncertaintyKind.UNATTRIBUTED_VENUE_OFFER:
            if venue_offer_id is None:
                raise ValueError("unattributed venue offer requires venue_offer_id")
            return _nonempty(venue_offer_id, field="venue_offer_id")
        if venue_offer_id is not None:
            raise ValueError(f"{kind.value} must not carry venue_offer_id")
        return None

    @staticmethod
    async def _require_attempt_scope(
        session: AsyncSession,
        *,
        attempt_id: UUID,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> None:
        attempt = await session.get(SubmissionAttemptRow, attempt_id)
        if attempt is None:
            raise ValueError("linked submission attempt does not exist")
        if (
            attempt.exchange_account_id != exchange_account_id
            or attempt.deployment_environment != deployment_environment
            or attempt.symbol != symbol
        ):
            raise ValueError("linked submission attempt does not match uncertainty scope")

    @staticmethod
    async def _require_venue_offer_scope(
        session: AsyncSession,
        *,
        venue_offer_id: str,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> None:
        venue_offer = await session.get(
            VenueOfferStateRow,
            (exchange_account_id, deployment_environment, venue_offer_id),
        )
        if venue_offer is None:
            raise ValueError("linked venue offer does not exist")
        if venue_offer.symbol != symbol:
            raise ValueError("linked venue offer does not match uncertainty scope")

    @staticmethod
    async def _require_event(
        session: AsyncSession,
        *,
        event_seq: int,
        exchange_account_id: UUID,
        deployment_environment: str,
    ) -> EventLogRow:
        event = await session.get(EventLogRow, event_seq)
        if (
            event is None
            or event.exchange_account_id != exchange_account_id
            or event.deployment_environment != deployment_environment
        ):
            raise ValueError("event sequence does not match account/environment scope")
        return event
