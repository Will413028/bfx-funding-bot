"""What the effects of an accepted cycle read: per-symbol totals and foreign live offers.

Both are views of the scope's latest accepted basis and its offer mirror; neither reads
history. ``PositionView`` (the operator console's) stays as it is: it has no foreign
amount and no offer count, which the NAV signal needs.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    AcceptedPositions,
    AcceptedSymbolPosition,
    ForeignOffers,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import reads
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis
from bfx_funding_bot.modules.ledger._internal.operator_resolution import attempt_match
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    VenueOfferMirrorRow,
)


async def accepted_positions(session: AsyncSession, scope: Scope) -> AcceptedPositions | None:
    basis = await previous_basis(session, scope)
    if basis is None:
        return None
    # ``previous_basis`` loads only the identity columns; the stamp is read by key.
    accepted_at_ms = await session.scalar(
        select(AcceptedCapitalBasisRow.accepted_at_ms).where(AcceptedCapitalBasisRow.id == basis.id)
    )
    assert accepted_at_ms is not None
    credit_counts = dict(
        (
            await session.execute(
                select(AcceptedCapitalBasisCreditRow.symbol, func.count())
                .where(AcceptedCapitalBasisCreditRow.basis_id == basis.id)
                .group_by(AcceptedCapitalBasisCreditRow.symbol)
            )
        )
        .tuples()
        .all()
    )
    offer_counts = dict(
        (
            await session.execute(
                select(VenueOfferMirrorRow.symbol, func.count())
                .where(
                    VenueOfferMirrorRow.exchange_account_id == scope.exchange_account_id,
                    VenueOfferMirrorRow.deployment_environment == scope.deployment_environment,
                    VenueOfferMirrorRow.present_in_latest_accepted_snapshot,
                )
                .group_by(VenueOfferMirrorRow.symbol)
            )
        )
        .tuples()
        .all()
    )
    rows = await session.execute(
        select(
            AcceptedCapitalBasisSymbolRow.symbol,
            AcceptedCapitalBasisSymbolRow.available,
            AcceptedCapitalBasisSymbolRow.offered,
            AcceptedCapitalBasisSymbolRow.foreign_offers,
            AcceptedCapitalBasisSymbolRow.credits,
        )
        .where(AcceptedCapitalBasisSymbolRow.basis_id == basis.id)
        .order_by(AcceptedCapitalBasisSymbolRow.symbol)
    )
    return AcceptedPositions(
        basis.observation_id,
        accepted_at_ms,
        tuple(
            AcceptedSymbolPosition(
                symbol, available, offered, foreign, credits,
                credit_counts.get(symbol, 0), offer_counts.get(symbol, 0),
            )
            for symbol, available, offered, foreign, credits in rows.tuples()
        ),
    )


async def foreign_live_offers(session: AsyncSession, scope: Scope) -> ForeignOffers:
    """Foreign live offers that no open UNKNOWN submit of the scope could be.

    A candidate is what ``match_unknown`` finds of the latest accepted observation: it may be
    the offer the UNKNOWN sent, so it is not called foreign while the UNKNOWN is open.
    """
    foreign, live_ids = await reads.live_foreign(session, scope)
    if not foreign:
        return ForeignOffers((), live_ids)
    basis = await previous_basis(session, scope)
    assert basis is not None  # a live mirror row exists only under an accepted observation
    candidates: set[str] = set()
    for attempt_id in await reads.open_unknown_attempt_ids(session, scope):
        match = await attempt_match(session, scope, attempt_id, basis.observation_id)
        if match is not None:
            candidates.update(match.candidate_venue_offer_ids)
    return ForeignOffers(
        tuple(offer for offer in foreign if offer.venue_offer_id not in candidates), live_ids
    )


__all__ = ["accepted_positions", "foreign_live_offers"]
