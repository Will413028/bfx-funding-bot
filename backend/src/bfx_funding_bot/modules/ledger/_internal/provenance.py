"""Venue-offer provenance shared by the basis classifier and the managed-offer reads.

Provenance of a venue offer: a transport ``ack`` outcome
(``ix_transport_outcome_venue_offer``) or a ``bound_to_venue`` resolution
(``ix_execution_resolution_venue_offer``) names it -> the attempt, whose
``cell_id`` the scope trigger proved equal to its decision's. No provenance
means the offer is foreign; more than one story is a conflict, never a reason to
call it foreign or to pick one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)


async def offer_provenance(
    session: AsyncSession, scope: Scope, venue_ids: Sequence[str]
) -> dict[str, set[UUID]]:
    """One batch: every attempt of this scope naming any of ``venue_ids``."""
    provenance: dict[str, set[UUID]] = {venue_id: set() for venue_id in venue_ids}
    if not venue_ids:
        return provenance
    acked = await session.scalars(
        select(TransportOutcomeJournalRow)
        .join(
            SubmissionAttemptJournalRow,
            SubmissionAttemptJournalRow.attempt_id == TransportOutcomeJournalRow.attempt_id,
        )
        .where(
            TransportOutcomeJournalRow.kind == "ack",
            TransportOutcomeJournalRow.venue_offer_id.in_(venue_ids),
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
        )
    )
    for outcome in acked:
        assert outcome.venue_offer_id is not None
        provenance[outcome.venue_offer_id].add(outcome.attempt_id)
    bound = await session.scalars(
        select(ExecutionResolutionJournalRow).where(
            ExecutionResolutionJournalRow.action == "bound_to_venue",
            ExecutionResolutionJournalRow.venue_offer_id.in_(venue_ids),
            ExecutionResolutionJournalRow.attempt_id.is_not(None),
            ExecutionResolutionJournalRow.exchange_account_id == scope.exchange_account_id,
            ExecutionResolutionJournalRow.deployment_environment == scope.deployment_environment,
        )
    )
    for resolution in bound:
        assert resolution.venue_offer_id is not None and resolution.attempt_id is not None
        provenance[resolution.venue_offer_id].add(resolution.attempt_id)
    return provenance


def sole_owner(
    attempt_ids: set[UUID],
    attempts: Mapping[UUID, SubmissionAttemptJournalRow],
    scope: Scope,
    symbol: str,
) -> SubmissionAttemptJournalRow | None:
    """The one attempt that placed an offer of ``symbol``; None when foreign.

    Raises ``LookupError`` on contradictory provenance: several attempts, or one
    of another scope or symbol.
    """
    if not attempt_ids:
        return None
    if len(attempt_ids) != 1:
        raise LookupError("provenance_conflict")
    attempt = attempts.get(next(iter(attempt_ids)))
    if attempt is None or (
        attempt.exchange_account_id,
        attempt.deployment_environment,
        attempt.symbol,
    ) != (scope.exchange_account_id, scope.deployment_environment, symbol):
        raise LookupError("provenance_conflict")
    return attempt


__all__ = ["offer_provenance", "sole_owner"]
