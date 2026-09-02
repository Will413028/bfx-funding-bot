"""Domain persistence boundary for submission attempts and uncertainties."""
from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
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

        async with self._session_factory() as session:
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
            position = await session.get(
                PositionStateRow, (exchange_account_id, environment, scoped_symbol)
            )
            if position is None:
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
                venue_offer_id=venue_offer_id,
                opened_event_seq=opened_event_seq,
            )
            position.uncertain_amount = Decimal(str(position.uncertain_amount)) + amount
            position.last_event_seq = max(position.last_event_seq, opened_event_seq)
            session.add(row)
            await session.commit()
            return row

    async def resolve(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
        kind: UncertaintyKind | str,
        reconcile_event_seq: int,
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
            position = await session.get(
                PositionStateRow, (exchange_account_id, environment, scoped_symbol)
            )
            if position is None:
                raise ValueError("position state is required before resolving an uncertainty")
            projected_amount = Decimal(str(position.uncertain_amount)) - Decimal(
                str(row.intended_amount)
            )
            if projected_amount < 0:
                raise ValueError("uncertain projection cannot become negative")

            row.state = "resolved"
            row.resolved_event_seq = reconcile_event_seq
            row.resolved_by_operator_id = operator_id
            row.resolution_reason = reason
            row.resolution_evidence = resolution_evidence
            row.resolved_at = reconcile_event.recorded_at
            position.uncertain_amount = projected_amount
            position.last_event_seq = max(position.last_event_seq, reconcile_event_seq)
            await session.commit()
            return row

    @staticmethod
    def _require_same_attempt(
        row: SubmissionAttemptRow, payload: SubmissionAttemptPayload
    ) -> None:
        if (
            row.exchange_account_id != payload.account_id
            or row.deployment_environment != payload.environment
            or row.symbol != payload.symbol
            or row.cid != payload.cid
            or row.payload_sha256 != payload.payload_fingerprint
        ):
            raise ValueError("execution decision already has a different immutable attempt")

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
