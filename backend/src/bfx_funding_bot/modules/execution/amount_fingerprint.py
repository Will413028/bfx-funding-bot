"""Fingerprints the legacy authority's open claims and unresolved attempts hold.

The amount codec (what a fingerprint is and how the planner chooses one) lives in
``execution.deployment.fingerprinted_amount``; this module only reads which
fingerprints the legacy tables still hold.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.trading import fingerprint_of

# An UNKNOWN claim stays UNKNOWN for audit after its attempt is resolved as not
# sent; while it is unresolved its attempt holds the fingerprint (below).
_OPEN_CLAIM_STATES = (RegistryState.PENDING.value, RegistryState.CLAIMED.value)


async def fingerprints_in_use(session: AsyncSession, *, account_id: UUID, environment: str,
                              symbol: str) -> frozenset[int]:
    """Fingerprints held by the symbol's non-terminal claims and unresolved attempts.

    An attempt stays unresolved while it has no outcome or its UNKNOWN is still
    open; one resolved as not sent keeps ``outcome_kind='unknown'`` for audit but
    frees its fingerprint.
    """
    claims = (await session.scalars(select(OfferClaimRow.size_usdt).where(
        OfferClaimRow.exchange_account_id == account_id,
        OfferClaimRow.deployment_environment == environment,
        OfferClaimRow.symbol == symbol,
        OfferClaimRow.state.in_(_OPEN_CLAIM_STATES),
    ))).all()
    open_unknown = select(ExecutionUncertaintyRow.attempt_id).where(
        ExecutionUncertaintyRow.exchange_account_id == account_id,
        ExecutionUncertaintyRow.deployment_environment == environment,
        ExecutionUncertaintyRow.symbol == symbol,
        ExecutionUncertaintyRow.kind == "submit_outcome_unknown",
        ExecutionUncertaintyRow.state == "open",
    )
    attempts = (await session.scalars(select(SubmissionAttemptRow.normalized_payload).where(
        SubmissionAttemptRow.exchange_account_id == account_id,
        SubmissionAttemptRow.deployment_environment == environment,
        SubmissionAttemptRow.symbol == symbol,
        (SubmissionAttemptRow.outcome_kind.is_(None))
        | ((SubmissionAttemptRow.outcome_kind == "unknown")
           & SubmissionAttemptRow.attempt_id.in_(open_unknown)),
    ))).all()
    held = [fingerprint_of(amount) for amount in claims]
    held += [fingerprint_of(payload.get("amount")) if isinstance(payload, dict) else None
             for payload in attempts]
    return frozenset(value for value in held if value)


__all__ = [
    "fingerprints_in_use",
]
