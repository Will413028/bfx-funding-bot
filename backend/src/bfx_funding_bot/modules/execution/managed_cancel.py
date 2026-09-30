"""Cancel the offers this bot placed, by venue id, through the command gate.

Lending envelope ADR 2026-09-25 D2/D3/D4: while the account is HALTED or a
currency is disabled, the planner converges that currency's *managed* offers
-- the ones a durable intent traces to (``venue_offer_state.execution_decision_id``)
-- to none, every tick (``DeploymentReconciler._pull_if_stopped``). Offers placed
by hand or by Bitfinex auto-renew are never touched; only the operator's kill,
which is a venue cancel-all, reaches them.

Each cancel goes through the normal cancel path (``CancelPort``, i.e. the
command gate): it is made durable before the venue call and refused for an
offer whose scope has an open or unreadable uncertainty. A refused or failed
cancel is reported, never retried here; the next sweep tries again.
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.protocols import AccountContext, CancelPort
from bfx_funding_bot.modules.ledger import LiveManagedOffer, ManagedOfferReader, Scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    requested: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


class ManagedOfferSweep:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
                 environment: str, canceller: CancelPort, ctx: AccountContext,
                 offers: ManagedOfferReader) -> None:
        self._sf = session_factory
        self._scope = Scope(account_id, environment)
        self._canceller = canceller
        self._ctx = ctx
        self._offers = offers

    async def managed_open(
        self, symbols: Collection[str] | None = None,
    ) -> tuple[LiveManagedOffer, ...]:
        async with self._sf() as session:
            return await self._offers.live(session, self._scope, symbols)

    async def cancel(self, symbols: Collection[str] | None = None, *, reason: str) -> SweepResult:
        """Cancel every open managed offer (of ``symbols``, or all of them)."""
        requested: list[str] = []
        failed: list[tuple[str, str]] = []
        for offer in await self.managed_open(symbols):
            try:
                correlation = UUID(offer.signal_correlation_id) if offer.signal_correlation_id else uuid4()
                await self._canceller.cancel(venue_offer_id=offer.venue_offer_id,
                                             signal_correlation_id=correlation,
                                             account_id=self._ctx.account_id, ctx=self._ctx)
            except Exception as exc:  # reported; the next sweep tries again
                failed.append((offer.venue_offer_id, f"{type(exc).__name__}: {exc}"[:200]))
            else:
                requested.append(offer.venue_offer_id)
        if requested or failed:
            log.warning("managed_offer_sweep reason=%s requested=%s failed=%s", reason,
                        requested, failed)
        return SweepResult(tuple(requested), tuple(failed))


__all__ = ["ManagedOfferSweep", "SweepResult"]
