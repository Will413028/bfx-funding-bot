"""Dormant operator evidence. Matching is deferred until S1-3c3c."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import (
    OBSERVATION_REF_PREFIX,
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


def observation_id(token: str) -> UUID:
    if not token.startswith(OBSERVATION_REF_PREFIX):
        raise ResolutionRejected("stale_reconcile_fence")
    suffix = token[len(OBSERVATION_REF_PREFIX) :]
    try:
        value = UUID(suffix)
    except ValueError as exc:
        raise ResolutionRejected("stale_reconcile_fence") from exc
    if str(value) != suffix:
        raise ResolutionRejected("stale_reconcile_fence")
    return value


async def latest_accepted(session: AsyncSession, scope: Scope) -> LedgerObservationRow | None:
    """The scope's latest accepted observation, with only its ``id`` loaded.

    Query revision is the durable order in which queries began, even if their
    replies complete out of order. Failed/unaccepted queries are not evidence.
    Explicit columns: the web API reads under a column allowlist that excludes
    ``evidence``. Callers use ``id`` only, and (no ``populate_existing``) a row
    the session already holds keeps its other columns loaded.
    """
    latest: LedgerObservationRow | None = await session.scalar(
        select(LedgerObservationRow)
        .options(load_only(LedgerObservationRow.id))
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
    )
    return latest


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
        # Explicit columns throughout: the web API holds a column allowlist.
        observation = (
            await session.execute(
                select(
                    LedgerObservationRow.accepted,
                    LedgerObservationRow.exchange_account_id,
                    LedgerObservationRow.deployment_environment,
                    LedgerObservationRow.query_id,
                    LedgerObservationRow.query_finished_at_ms,
                    LedgerObservationRow.first_digest,
                    LedgerObservationRow.confirmation_digest,
                    LedgerObservationRow.wallets_complete,
                    LedgerObservationRow.offers_complete,
                    LedgerObservationRow.credits_complete,
                    LedgerObservationRow.loans_complete,
                    LedgerObservationRow.offer_history_complete,
                    LedgerObservationRow.credit_history_complete,
                    LedgerObservationRow.trades_complete,
                ).where(LedgerObservationRow.id == identifier)
            )
        ).one_or_none()
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
        if subject.attempt_id is not None:
            opening = (
                await session.execute(
                    select(
                        SubmissionAttemptJournalRow.exchange_account_id,
                        SubmissionAttemptJournalRow.deployment_environment,
                        SubmissionAttemptJournalRow.symbol,
                        SubmissionAttemptJournalRow.started_at_ms.label("opened_at_ms"),
                    ).where(SubmissionAttemptJournalRow.attempt_id == subject.attempt_id)
                )
            ).one_or_none()
        else:
            opening = (
                await session.execute(
                    select(
                        QuarantineOpeningRow.exchange_account_id,
                        QuarantineOpeningRow.deployment_environment,
                        QuarantineOpeningRow.symbol,
                        QuarantineOpeningRow.opened_at_ms,
                    ).where(QuarantineOpeningRow.quarantine_id == subject.uncertainty_id)
                )
            ).one_or_none()
        if (
            opening is None
            or opening.exchange_account_id != scope.exchange_account_id
            or opening.deployment_environment != scope.deployment_environment
            or opening.symbol != subject.symbol
        ):
            raise ResolutionRejected("not_found", kind="not_found")
        opened_at = opening.opened_at_ms
        query = (
            await session.execute(
                select(
                    LedgerObservationQueryRow.started_at_ms,
                    LedgerObservationQueryRow.exchange_account_id,
                    LedgerObservationQueryRow.deployment_environment,
                ).where(LedgerObservationQueryRow.query_id == observation.query_id)
            )
        ).one_or_none()
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
