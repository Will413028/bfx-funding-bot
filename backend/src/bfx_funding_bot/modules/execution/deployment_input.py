"""What the periodic deployment may reprice: the offers this bot owns.

``PeriodicReconcile`` deploys from an accepted cycle; the managed offers are read from
the ledger's live offer mirror of the cycle's accepted snapshot. Only *managed* offers
are the bot's to reprice (D2): a foreign offer is never cancelled, an UNKNOWN's candidate
has no claim a cancel could be admitted against, and an offer whose provenance
contradicts itself is neither.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment.tenor import (
    LongTenorState,
    resting_long_offers,
)
from bfx_funding_bot.modules.ledger import (
    CycleResult,
    LedgerManagedOffers,
    LongTenorQuery,
    ManagedOffer,
    Scope,
)


class DeploymentInput(Protocol):
    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        """The accepted ``cycle``'s offers the deployment may reprice."""
        ...

    async def long_tenor(self) -> LongTenorState | None:
        """The configured long tenor's exposure and resting offers; None when unconfigured."""
        ...


class LedgerDeploymentInput:
    """Managed live offers of the ledger's mirror, on a short session of their own.

    Reprice needs a rate, so an offer whose rate the venue did not report is
    left alone; so is one with contradictory provenance (the read separates
    those from the managed ones). Foreign offers are never in the managed set.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
                 offers: LedgerManagedOffers, long_tenor: LongTenorQuery | None = None) -> None:
        self._sf = session_factory
        self._scope = Scope(account_id)
        self._offers = offers
        self._long_tenor = long_tenor

    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        async with self._sf() as session, session.begin():
            found = await self._offers.managed_live_offers(session, self._scope)
        priced = (_active_offer(offer) for offer in found.offers)
        return tuple(offer for offer in priced if offer is not None)

    async def long_tenor(self) -> LongTenorState | None:
        if self._long_tenor is None:
            return None
        async with self._sf() as session, session.begin():
            # Every managed offer, priced or not: reprice skips an unpriced one, but
            # the one-at-a-time limit must still see it.
            found = await self._offers.managed_live_offers(
                session, self._scope, [self._long_tenor.symbol])
            return LongTenorState(
                exposure=await self._offers.long_tenor_exposure(
                    session, self._scope, self._long_tenor),
                resting=resting_long_offers(found, self._long_tenor),
            )


def _active_offer(offer: ManagedOffer) -> ActiveFundingOffer | None:
    if not offer.rate_observed or offer.rate is None or offer.period_days is None:
        return None
    return ActiveFundingOffer(
        venue_offer_id=offer.venue_offer_id,
        symbol=offer.symbol,
        amount=offer.amount_remaining,
        rate=float(offer.rate),
        period_days=offer.period_days,
        mts_created=offer.mts_created,
        status=offer.status,
        amount_original=offer.amount_original,
        rate_observed=True,
        rate_decimal=offer.rate,
        signal_correlation_id=offer.signal_correlation_id,
    )


__all__ = ["DeploymentInput", "LedgerDeploymentInput"]
