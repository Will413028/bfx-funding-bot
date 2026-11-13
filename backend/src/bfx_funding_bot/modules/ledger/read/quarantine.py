"""Unresolved quarantines of a scope, read only (openings are ``_internal.quarantine``)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import (
    Scope,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    ExecutionResolutionJournalRow,
    QuarantineOpeningRow,
)


async def has_unresolved_quarantine(
    session: AsyncSession, scope: Scope, basis: AcceptedCapitalBasisRow | None, symbol: str
) -> bool:
    """``unresolved_quarantines`` for one symbol as one bounded EXISTS (LIMIT 1)."""
    candidate = QuarantineOpeningRow.opened_revision > (
        basis.accept_revision if basis is not None else 0
    )
    if basis is not None:
        candidate = candidate | QuarantineOpeningRow.quarantine_id.in_(
            select(AcceptedCapitalBasisQuarantineRow.quarantine_id).where(
                AcceptedCapitalBasisQuarantineRow.basis_id == basis.id
            )
        )
    found = await session.scalar(
        select(QuarantineOpeningRow.quarantine_id)
        .where(
            QuarantineOpeningRow.exchange_account_id == scope.exchange_account_id,
            QuarantineOpeningRow.symbol == symbol,
            candidate,
            ~exists().where(
                ExecutionResolutionJournalRow.quarantine_id == QuarantineOpeningRow.quarantine_id
            ),
        )
        .limit(1)
    )
    return found is not None


async def unresolved_quarantines(
    session: AsyncSession,
    scope: Scope,
    basis: AcceptedCapitalBasisRow | None,
    *,
    symbol: str | None = None,
) -> list[QuarantineOpeningRow]:
    """Quarantines of the scope without a resolution, ordered by opening.

    Bounded by ``basis`` (the latest accepted one): an opening at or before its
    ``accept_revision`` that it did not list was resolved by then, and a
    resolution is permanent. So the candidates are the basis's listed
    quarantines plus openings with ``opened_revision > accept_revision``;
    without a basis, every opening of the scope.
    """
    accept_revision = basis.accept_revision if basis is not None else 0
    # Explicit columns: the web API reads this table under a column allowlist
    # that excludes ``evidence``. Callers use only these.
    columns = load_only(
        QuarantineOpeningRow.quarantine_id,
        QuarantineOpeningRow.symbol,
        QuarantineOpeningRow.intended_amount,
        QuarantineOpeningRow.opened_at_ms,
        QuarantineOpeningRow.source_attempt_id,
    )
    since = select(QuarantineOpeningRow).options(columns).where(
        QuarantineOpeningRow.exchange_account_id == scope.exchange_account_id,
        QuarantineOpeningRow.opened_revision > accept_revision,
    )
    if symbol is not None:
        since = since.where(QuarantineOpeningRow.symbol == symbol)
    candidates = {row.quarantine_id: row for row in await session.scalars(since)}
    if basis is not None:
        listed = (
            select(QuarantineOpeningRow)
            .options(columns)
            .join(
                AcceptedCapitalBasisQuarantineRow,
                AcceptedCapitalBasisQuarantineRow.quarantine_id
                == QuarantineOpeningRow.quarantine_id,
            )
            .where(AcceptedCapitalBasisQuarantineRow.basis_id == basis.id)
        )
        if symbol is not None:
            listed = listed.where(QuarantineOpeningRow.symbol == symbol)
        for row in await session.scalars(listed):
            candidates[row.quarantine_id] = row
    if not candidates:
        return []
    resolved: set[UUID | None] = set(
        await session.scalars(
            select(ExecutionResolutionJournalRow.quarantine_id).where(
                ExecutionResolutionJournalRow.quarantine_id.in_(sorted(candidates))
            )
        )
    )
    return sorted(
        (row for quarantine_id, row in candidates.items() if quarantine_id not in resolved),
        key=lambda row: (row.opened_at_ms, row.quarantine_id),
    )

