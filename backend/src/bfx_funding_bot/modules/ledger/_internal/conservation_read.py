"""The per-symbol conservation verdicts the scope's latest accepted basis stored."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis
from bfx_funding_bot.modules.ledger.conservation import (
    CONSERVATION_VERDICTS,
    AcceptedConservation,
    SymbolConservation,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisSymbolRow,
    LedgerObservationRow,
)


async def latest_conservation(session: AsyncSession, scope: Scope) -> AcceptedConservation | None:
    """None when the scope has no accepted basis. Bounded by the basis's symbol rows."""
    basis = await previous_basis(session, scope)
    if basis is None:
        return None
    query_id = await session.scalar(
        select(LedgerObservationRow.query_id).where(LedgerObservationRow.id == basis.observation_id)
    )
    assert query_id is not None
    rows = await session.scalars(
        select(AcceptedCapitalBasisSymbolRow)
        .where(AcceptedCapitalBasisSymbolRow.basis_id == basis.id)
        .order_by(AcceptedCapitalBasisSymbolRow.symbol)
    )
    symbols: list[SymbolConservation] = []
    for row in rows:
        verdict = next((v for v in CONSERVATION_VERDICTS if v == row.conservation), None)
        if verdict is None:
            raise ValueError(f"unknown conservation verdict {row.conservation!r}")
        symbols.append(
            SymbolConservation(row.symbol, verdict, row.lent_unexplained, row.foreign_executed)
        )
    return AcceptedConservation(basis.id, query_id, tuple(symbols))


__all__ = ["latest_conservation"]
