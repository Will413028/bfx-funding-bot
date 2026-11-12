"""The latest accepted capital basis of a scope, read only (the writer is ``_internal.basis``)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
)


async def previous_basis(session: AsyncSession, scope: Scope) -> AcceptedCapitalBasisRow | None:
    """The latest accepted basis of the scope (by its query revision).

    The query's own scope columns are repeated (the basis and observation insert
    triggers already make them equal to the basis's) so the planner can walk
    ``uq_ledger_observation_query_scope_revision`` backward (newest-first) and stop at the
    first query with a basis, instead of sorting every basis of the scope.
    """
    row: AcceptedCapitalBasisRow | None = await session.scalar(
        select(AcceptedCapitalBasisRow)
        .join(
            LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id
        )
        .join(
            LedgerObservationQueryRow,
            LedgerObservationQueryRow.query_id == LedgerObservationRow.query_id,
        )
        .where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
            AcceptedCapitalBasisRow.exchange_account_id == scope.exchange_account_id,
            AcceptedCapitalBasisRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
        .options(
            load_only(
                AcceptedCapitalBasisRow.id,
                AcceptedCapitalBasisRow.observation_id,
                AcceptedCapitalBasisRow.accept_revision,
                AcceptedCapitalBasisRow.attempt_seq_high_water,
            )
        )
    )
    return row



__all__ = [
    "previous_basis",
]
