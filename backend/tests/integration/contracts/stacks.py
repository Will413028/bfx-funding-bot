"""Authority-neutral scenario builders over the legacy and the ledger stacks.

A contract test states a scenario once (``policy``, ``snapshot``, ``place``,
``unknown``, ``quarantine``) and asserts port-level observables on the four
consumer read ports. Each stack writes through its own authority's write paths:

* ``legacy``: ``CapitalRepository`` and its event writer (the existing
  ``test_capital_repository`` helpers);
* ``ledger``: ``LedgerJournal`` and ``LedgerObservations.accept`` (the ``Book``
  of ``test_ledger_capital_reader``).

Both stacks share one migrated database per test and one clock: snapshots
start at 1000 and finish at 1050/1060, reads happen at ``NOW``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationUnknown,
    VenueOfferQuarantined,
)
from bfx_funding_bot.modules.execution.legacy_ports import (
    LegacyCapitalAuthority,
    LegacyManagedOffers,
    LegacyScopeLock,
    LegacyUncertaintyReader,
)
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    ManagedOfferReader,
    Scope,
    ScopeLock,
    UncertaintyReader,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_managed_offer_reader,
    build_scope_lock,
    build_uncertainty_reader,
)
from bfx_funding_bot.modules.trading import CapitalPolicy, CapitalScope

from ..test_capital_repository import authorize, repository, snapshot
from ..test_ledger_basis import _observation, _offer
from ..test_ledger_capital_reader import _ATTEMPT_POLICY, Book

ACCOUNT = UUID("00000000-0000-0000-0000-00000000a001")
ENVIRONMENT = "ci"
SCOPE = Scope(ACCOUNT, ENVIRONMENT)
NOW = 1100
MAX_SNAPSHOT_AGE_MS = 10_000
CELL = "a30"


@dataclass(frozen=True)
class Venue:
    """One venue offer a snapshot reports (managed iff ``place`` named it first)."""

    venue_offer_id: str
    amount: str
    symbol: str = "fUST"


@dataclass(frozen=True)
class Stack:
    """The four ports plus the scenario builders of one authority."""

    name: str
    factory: async_sessionmaker[Any]
    capital: CapitalAuthority
    uncertainties: UncertaintyReader
    offers: ManagedOfferReader
    lock: ScopeLock
    builders: Any

    @property
    def scope(self) -> Scope:
        return SCOPE

    def capital_scope(self, symbol: str = "fUST", cell: str = CELL) -> CapitalScope:
        return CapitalScope(ACCOUNT, ENVIRONMENT, symbol, cell)

    async def policy(
        self, symbol: str = "fUST", *, reserve: str = "100", enabled: bool = True
    ) -> None:
        """Apply the symbol's policy (legacy supports fUST enabled, fUSD only disabled)."""
        await self.builders.policy(symbol, reserve, enabled)

    async def snapshot(self, available: str = "1000", *, offers: tuple[Venue, ...] = ()) -> None:
        """An accepted, confirmed snapshot (fUST wallet ``available``) reporting ``offers``."""
        await self.builders.snapshot(available, offers)

    async def place(self, venue_offer_id: str, amount: str, *, symbol: str = "fUST") -> None:
        """Authorize and acknowledge one offer of cell a30 against the latest snapshot."""
        await self.builders.place(venue_offer_id, amount, symbol)

    async def unknown(self, amount: str, *, symbol: str = "fUST") -> None:
        """A submit whose outcome is UNKNOWN and unresolved."""
        await self.builders.unknown(amount, symbol)

    async def quarantine(self, symbol: str = "fUST", *, amount: str = "10") -> None:
        """An open quarantine (an unattributed venue offer)."""
        await self.builders.quarantine(symbol, amount)


class _LegacyBuilders:
    def __init__(self, factory: async_sessionmaker[Any]) -> None:
        self.factory = factory
        self.repo = repository(ACCOUNT, ENVIRONMENT)
        self.policies: dict[str, Any] = {}
        self.seq = 0
        self.cids = iter(range(1, 1000))
        self.pending: dict[str, VenueOfferObservation] = {}
        self.unknown_refs: list[Any] = []

    async def policy(self, symbol: str, reserve: str, enabled: bool) -> None:
        async with self.factory.begin() as session:
            self.policies[symbol] = await self.repo.apply_policy(
                session, symbol=symbol,
                policy=CapitalPolicy(
                    enabled=enabled, reserve_amount=Decimal(reserve),
                    max_cell_fraction=Decimal(1),
                ),
                expected_revision=0, source={"operator": "contract"},
            )

    async def snapshot(self, available: str, offers: tuple[Venue, ...]) -> None:
        observed = tuple(
            self.pending.get(o.venue_offer_id)
            or VenueOfferObservation(
                o.venue_offer_id, o.symbol, Decimal(o.amount), Decimal(o.amount),
                Decimal("0.0001"), 2, "active", 1000, 1000,
            )
            for o in offers
        )
        self.seq = await snapshot(self.factory, self.repo, available, offers=observed)

    async def place(self, venue_offer_id: str, amount: str, symbol: str) -> None:
        cid = next(self.cids)
        result = await authorize(
            self.factory, self.repo, self.policies[symbol], self.seq, amount, cid, cell=CELL
        )
        async with self.factory.begin() as session:
            await self.repo.writer.append(session, ReservationClaimed(
                symbol=symbol, cid=cid, signal_correlation_id=result.intent.signal_correlation_id,
                account_id=str(ACCOUNT), is_simulated=True, amount=Decimal(amount),
                venue_offer_id=venue_offer_id,
                reservation_ref=replace(
                    result.intent.reservation_ref, venue_offer_id=venue_offer_id
                ),
                occurred_at_ms=1100,
            ))
        # The daemon enriches the observation with the claim's decision; that is
        # what makes the projected offer managed.
        self.pending[venue_offer_id] = VenueOfferObservation(
            venue_offer_id, symbol, Decimal(amount), Decimal(amount), Decimal("0.0001"), 2,
            "active", 1000, 1100,
            execution_decision_id=result.intent.execution_decision_id,
            signal_correlation_id=result.intent.signal_correlation_id,
        )

    async def unknown(self, amount: str, symbol: str) -> None:
        cid = next(self.cids)
        result = await authorize(
            self.factory, self.repo, self.policies[symbol], self.seq, amount, cid, cell=CELL
        )
        async with self.factory.begin() as session:
            await self.repo.writer.append(session, ReservationUnknown(
                symbol=symbol, cid=cid, account_id=str(ACCOUNT), is_simulated=True,
                signal_correlation_id=result.intent.signal_correlation_id,
                reservation_ref=result.intent.reservation_ref, amount=Decimal(amount),
                reason="contract", occurred_at_ms=1150,
            ))

    async def quarantine(self, symbol: str, amount: str) -> None:
        offer = VenueOfferObservation(
            f"orphan-{symbol}", symbol, Decimal(amount), Decimal(amount), Decimal("0.0001"), 2,
            "active", 1000, 1000,
        )
        self.seq = await snapshot(self.factory, self.repo, "1000", offers=(offer,))
        async with self.factory.begin() as session:
            await self.repo.writer.append(session, VenueOfferQuarantined(
                venue_offer_id=offer.venue_offer_id, symbol=symbol, amount=Decimal(amount),
                account_id=str(ACCOUNT), occurred_at_ms=1100,
            ))


class _LedgerBuilders:
    def __init__(self, factory: async_sessionmaker[Any]) -> None:
        self.book = Book(factory, SCOPE)
        self.factory = factory

    async def policy(self, symbol: str, reserve: str, enabled: bool) -> None:
        await self.book.policy(symbol, reserve=reserve, enabled=enabled)

    async def snapshot(self, available: str, offers: tuple[Venue, ...]) -> None:
        observation = _observation(
            available,
            usd=True,  # the legacy wallet always reports fUSD (0)
            offers=tuple(
                _offer(o.venue_offer_id, o.amount, created=500, symbol=o.symbol) for o in offers
            ),
        )
        assert await self.book.accept(
            observation, started=1000, finished=1050, confirmed=1060
        ) == "accepted"

    async def place(self, venue_offer_id: str, amount: str, symbol: str) -> None:
        await self.book.attempt(
            amount, outcome="ack", venue_offer_id=venue_offer_id, cell=CELL, symbol=symbol
        )

    async def unknown(self, amount: str, symbol: str) -> None:
        await self.book.attempt(amount, outcome="unknown", cell=CELL, symbol=symbol)

    async def quarantine(self, symbol: str, amount: str) -> None:
        # The ledger's quarantine carries its own intended amount (10): see Book.quarantine.
        del amount
        await self.book.quarantine(symbol)


async def seed_account(sync_engine: Any) -> None:
    """The one account and the policy revision attempts must name (see ``book``)."""
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','contract')"),
            {"id": str(ACCOUNT)},
        )
        conn.execute(
            text(
                "INSERT INTO capital_policy_revisions(id, exchange_account_id, "
                "deployment_environment, symbol, revision, schema_version, policy, digest, "
                "source) VALUES (:p, :a, 'ci', 'fATTEMPT', 1, 1, '{}', 'p', '{}')"
            ),
            {"p": _ATTEMPT_POLICY, "a": str(ACCOUNT)},
        )


def build_stack(name: str, factory: async_sessionmaker[Any]) -> Stack:
    if name == "legacy":
        builders = _LegacyBuilders(factory)
        runtime = CapitalRuntime(
            repository=builders.repo, session_factory=factory, clock=lambda: NOW
        )
        return Stack(
            name, factory, LegacyCapitalAuthority(runtime), LegacyUncertaintyReader(factory),
            LegacyManagedOffers(), LegacyScopeLock(builders.repo), builders,
        )
    assert name == "ledger"
    return Stack(
        name, factory,
        build_capital_authority(factory, max_snapshot_age_ms=MAX_SNAPSHOT_AGE_MS),
        build_uncertainty_reader(factory), build_managed_offer_reader(), build_scope_lock(),
        _LedgerBuilders(factory),
    )
