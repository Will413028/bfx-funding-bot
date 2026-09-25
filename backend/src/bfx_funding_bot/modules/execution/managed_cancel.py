"""Cancel the offers this bot placed, by venue id, through the command gate.

Lending envelope ADR 2026-09-25 D2/D3/D4: an automatic stop (ladder level 3)
and a disabled currency pull only *managed* offers -- the ones a durable intent
traces to (``venue_offer_state.execution_decision_id``). Offers placed by hand
or by Bitfinex auto-renew are never touched; only the operator's kill, which
is a venue cancel-all, reaches them.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.event_store.tables import VenueOfferStateRow
from bfx_funding_bot.modules.execution.protocols import AccountContext, CancelPort

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    requested: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


class ManagedOfferSweep:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
                 environment: str, canceller: CancelPort, ctx: AccountContext) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._environment = environment
        self._canceller = canceller
        self._ctx = ctx

    async def managed_open(self, symbols: Collection[str] | None = None) -> list[VenueOfferStateRow]:
        async with self._sf() as session:
            query = select(VenueOfferStateRow).where(
                VenueOfferStateRow.exchange_account_id == self._account_id,
                VenueOfferStateRow.deployment_environment == self._environment,
                VenueOfferStateRow.is_terminal.is_(False),
                VenueOfferStateRow.execution_decision_id.is_not(None),
            )
            if symbols is not None:
                query = query.where(VenueOfferStateRow.symbol.in_(list(symbols)))
            return list((await session.scalars(query.order_by(VenueOfferStateRow.venue_offer_id))).all())

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
