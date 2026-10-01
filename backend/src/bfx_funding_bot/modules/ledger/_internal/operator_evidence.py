"""Dormant operator evidence. Matching is deferred until S1-3c3c."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    ResolutionEvidence,
    ResolutionRejected,
    ResolutionSubject,
    Scope,
    VerifiedEvidence,
)
from bfx_funding_bot.modules.ledger.tables import (
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
)

_PREFIX = "ledger:v1:obs:"


def observation_id(token: str) -> UUID:
    if not token.startswith(_PREFIX):
        raise ResolutionRejected("stale_reconcile_fence")
    suffix = token[len(_PREFIX) :]
    try:
        value = UUID(suffix)
    except ValueError as exc:
        raise ResolutionRejected("stale_reconcile_fence") from exc
    if str(value) != suffix:
        raise ResolutionRejected("stale_reconcile_fence")
    return value


async def latest_accepted(session: AsyncSession, scope: Scope) -> LedgerObservationRow | None:
    # Query revision is the durable order in which queries began, even if their
    # replies complete out of order. Failed/unaccepted queries are not evidence.
    result = await session.scalar(
        select(LedgerObservationRow)
        .join(
            LedgerObservationQueryRow,
            LedgerObservationQueryRow.query_id == LedgerObservationRow.query_id,
        )
        .where(
            LedgerObservationRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationRow.deployment_environment == scope.deployment_environment,
            LedgerObservationRow.accepted.is_(True),
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return result


class LedgerOperatorEvidence:
    async def verify(
        self,
        session: AsyncSession,
        scope: Scope,
        subject: ResolutionSubject,
        evidence_ref: str,
        *,
        require_history: bool,
    ) -> VerifiedEvidence:
        identifier = observation_id(evidence_ref)
        observation = await session.get(LedgerObservationRow, identifier, populate_existing=True)
        latest = await latest_accepted(session, scope)
        if (
            observation is None
            or not observation.accepted
            or latest is None
            or latest.id != identifier
            or observation.exchange_account_id != scope.exchange_account_id
            or observation.deployment_environment != scope.deployment_environment
        ):
            raise ResolutionRejected("stale_reconcile_fence")
        opening: SubmissionAttemptJournalRow | QuarantineOpeningRow | None
        if subject.attempt_id is not None:
            opening = await session.get(
                SubmissionAttemptJournalRow, subject.attempt_id, populate_existing=True
            )
            opened_at = opening.started_at_ms if opening is not None else None
        else:
            opening = await session.get(
                QuarantineOpeningRow, subject.uncertainty_id, populate_existing=True
            )
            opened_at = opening.opened_at_ms if opening is not None else None
        if (
            opening is None
            or opening.exchange_account_id != scope.exchange_account_id
            or opening.deployment_environment != scope.deployment_environment
            or opening.symbol != subject.symbol
        ):
            raise ResolutionRejected("not_found", kind="not_found")
        query = await session.get(
            LedgerObservationQueryRow, observation.query_id, populate_existing=True
        )
        if (
            query is None
            or opened_at is None
            or query.started_at_ms <= opened_at
            or query.exchange_account_id != scope.exchange_account_id
            or query.deployment_environment != scope.deployment_environment
            or observation.query_finished_at_ms < query.started_at_ms
            or observation.first_digest != observation.confirmation_digest
        ):
            raise ResolutionRejected("stale_reconcile_fence")
        if not all(
            (
                observation.wallets_complete,
                observation.offers_complete,
                observation.credits_complete,
                observation.loans_complete,
                observation.credit_history_complete,
                observation.trades_complete,
            )
        ):
            raise ResolutionRejected("incomplete_reconcile_coverage")
        # Ledger acceptance requires every stream, even for a manual resolution.
        if not observation.offer_history_complete:
            raise ResolutionRejected("incomplete_offer_history_coverage")
        return VerifiedEvidence(evidence_ref, query.started_at_ms, observation.query_finished_at_ms)

    async def resolution_context(
        self, session: AsyncSession, scope: Scope, subject: ResolutionSubject
    ) -> ResolutionEvidence:
        return ResolutionEvidence(unavailable_reason="matcher_pending")
