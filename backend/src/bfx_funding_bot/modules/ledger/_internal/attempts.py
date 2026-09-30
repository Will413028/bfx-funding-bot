"""Attempt-tail and evidence statements shared by the capital reader and the reads.

The attempts that can still be open are bounded by the latest basis: the ones it
classified ``unresolved`` plus the tail recorded after its
``attempt_seq_high_water`` (``uq_submission_attempt_scope_seq``). Outcomes (PK)
and resolutions (unique partial index on ``attempt_id``) are read for exactly
those ids.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

# Attempts recorded after the latest basis; a longer tail means acceptance has
# stalled, and reads refuse rather than grow with it.
MAX_TAIL_ATTEMPTS = 256

_ATTEMPT_COLUMNS = (
    SubmissionAttemptJournalRow.attempt_id,
    SubmissionAttemptJournalRow.exchange_account_id,
    SubmissionAttemptJournalRow.deployment_environment,
    SubmissionAttemptJournalRow.symbol,
    SubmissionAttemptJournalRow.cell_id,
    SubmissionAttemptJournalRow.attempt_seq,
    SubmissionAttemptJournalRow.normalized_payload,
)


async def fresh_all[T](session: AsyncSession, statement: Select[tuple[T]]) -> list[T]:
    # Fresh column values even if the caller's session already holds the rows.
    return list((await session.scalars(statement.execution_options(populate_existing=True))).all())


async def tail_attempts(
    session: AsyncSession,
    account: UUID,
    environment: str,
    high_water: int,
    *,
    limit: int,
) -> list[SubmissionAttemptJournalRow]:
    """Up to ``limit`` attempts with ``attempt_seq > high_water``, in sequence order."""
    return await fresh_all(
        session,
        select(SubmissionAttemptJournalRow)
        .options(load_only(*_ATTEMPT_COLUMNS))
        .where(
            SubmissionAttemptJournalRow.exchange_account_id == account,
            SubmissionAttemptJournalRow.deployment_environment == environment,
            SubmissionAttemptJournalRow.attempt_seq > high_water,
        )
        .order_by(SubmissionAttemptJournalRow.attempt_seq)
        .limit(limit),
    )


async def attempts_by_id(
    session: AsyncSession, attempt_ids: Sequence[UUID]
) -> list[SubmissionAttemptJournalRow]:
    if not attempt_ids:
        return []
    return await fresh_all(
        session,
        select(SubmissionAttemptJournalRow)
        .options(load_only(*_ATTEMPT_COLUMNS, SubmissionAttemptJournalRow.execution_decision_id))
        .where(SubmissionAttemptJournalRow.attempt_id.in_(sorted(set(attempt_ids)))),
    )


async def attempt_evidence(
    session: AsyncSession, attempt_ids: Sequence[UUID]
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Outcome kind and resolution action per attempt (absent when none)."""
    outcomes: dict[UUID, str] = {}
    resolutions: dict[UUID, str] = {}
    if not attempt_ids:
        return outcomes, resolutions
    for outcome in await fresh_all(
        session,
        select(TransportOutcomeJournalRow)
        .options(load_only(TransportOutcomeJournalRow.attempt_id, TransportOutcomeJournalRow.kind))
        .where(TransportOutcomeJournalRow.attempt_id.in_(attempt_ids)),
    ):
        outcomes[outcome.attempt_id] = outcome.kind
    for resolution in await fresh_all(
        session,
        select(ExecutionResolutionJournalRow)
        .options(
            load_only(
                ExecutionResolutionJournalRow.id,
                ExecutionResolutionJournalRow.attempt_id,
                ExecutionResolutionJournalRow.action,
            )
        )
        .where(ExecutionResolutionJournalRow.attempt_id.in_(attempt_ids)),
    ):
        assert resolution.attempt_id is not None
        resolutions[resolution.attempt_id] = resolution.action
    return outcomes, resolutions


def payload_amount(payload: object) -> Decimal | None:
    """The attempt's submitted ``amount``; None when missing, non-finite or negative."""
    try:
        amount = Decimal(str(payload["amount"]))  # type: ignore[index]
    except (KeyError, TypeError, InvalidOperation):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def open_unknowns(
    attempts: Iterable[tuple[UUID, str]], outcomes: dict[UUID, str], resolutions: dict[UUID, str]
) -> list[tuple[UUID, str]]:
    """``(attempt_id, symbol)`` of UNKNOWN outcomes that no resolution closed."""
    return [
        (attempt_id, symbol)
        for attempt_id, symbol in attempts
        if outcomes.get(attempt_id) == "unknown" and attempt_id not in resolutions
    ]


__all__ = [
    "MAX_TAIL_ATTEMPTS",
    "attempt_evidence",
    "attempts_by_id",
    "fresh_all",
    "open_unknowns",
    "payload_amount",
    "tail_attempts",
]
