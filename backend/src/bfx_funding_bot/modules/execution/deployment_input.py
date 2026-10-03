"""What the periodic deployment may reprice: the offers this bot owns, per authority.

``PeriodicReconcile`` deploys from an accepted cycle without knowing which
authority produced it. This port is the one place that differs: the legacy
cycle carries the venue snapshot it reconciled; under the ledger the managed
offers are read from the live offer mirror of the cycle's accepted snapshot.
Either way only *managed* offers are the bot's to reprice (D2): a foreign offer
is never cancelled, an UNKNOWN's candidate has no claim a cancel could be
admitted against, and an offer whose provenance contradicts itself is neither.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.observation_sink import LegacyCycleResult
from bfx_funding_bot.modules.ledger import (
    CycleResult,
    LedgerManagedOffers,
    ManagedOffer,
    Scope,
)


class DeploymentInput(Protocol):
    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        """The accepted ``cycle``'s offers the deployment may reprice."""
        ...


class LegacyDeploymentInput:
    """The reconcile snapshot's offers minus those the bot does not own."""

    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        if not isinstance(cycle, LegacyCycleResult):
            raise TypeError(f"legacy deployment input got {type(cycle).__name__}")
        result = cycle.legacy
        return tuple(
            offer for offer in result.venue_offers
            if offer.venue_offer_id not in result.unmanaged_offer_ids
        )


class LedgerDeploymentInput:
    """Managed live offers of the ledger's mirror, on a short session of their own.

    Reprice needs a rate, so an offer whose rate the venue did not report is
    left alone; so is one with contradictory provenance (the read separates
    those from the managed ones). Foreign offers are never in the managed set.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
                 environment: str, offers: LedgerManagedOffers) -> None:
        self._sf = session_factory
        self._scope = Scope(account_id, environment)
        self._offers = offers

    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        async with self._sf() as session, session.begin():
            found = await self._offers.managed_live_offers(session, self._scope)
        priced = (_active_offer(offer) for offer in found.offers)
        return tuple(offer for offer in priced if offer is not None)


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
    )


__all__ = ["DeploymentInput", "LedgerDeploymentInput", "LegacyDeploymentInput"]
