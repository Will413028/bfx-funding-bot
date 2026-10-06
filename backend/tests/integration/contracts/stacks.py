"""Scenario builders over the ledger stack, the only capital authority.

A contract test states a scenario once (``policy``, ``snapshot``, ``place``,
``unknown``, ``quarantine``) and asserts port-level observables on the four
consumer read ports. The stack writes through the ledger's write paths:
``LedgerJournal`` and ``LedgerObservations.accept`` (the ``Book`` of
``test_ledger_capital_reader``).

One migrated database per test and one clock: snapshots start at 1000 and finish at
1050/1060, reads happen at ``NOW``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.command_boundary import CommandBoundary, CommandEffects
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    CapitalAvailable,
    CommandAttempt,
    CommandJournal,
    ManagedOfferReader,
    Scope,
    ScopeLock,
    UncertaintyReader,
)
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    SubmissionAttemptJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_command_journal,
    build_managed_offer_reader,
    build_scope_lock,
    build_uncertainty_reader,
)
from bfx_funding_bot.modules.trading import CapitalScope

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
    journal: CommandJournal
    builders: Any

    @property
    def scope(self) -> Scope:
        return SCOPE

    def boundary(self, effects: CommandEffects) -> CommandBoundary:
        """The command gate's boundary over this stack's journal."""
        return CommandBoundary(SCOPE, self.factory, self.journal, effects)

    def capital_scope(self, symbol: str = "fUST", cell: str = CELL) -> CapitalScope:
        return CapitalScope(ACCOUNT, ENVIRONMENT, symbol, cell)

    async def policy(
        self, symbol: str = "fUST", *, reserve: str = "100", enabled: bool = True
    ) -> None:
        """Apply the symbol's policy."""
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

    async def token(self, symbol: str = "fUST") -> str:
        """The basis token a decision reads now (what the command gate carries to authorize)."""
        read = await self.capital.read(self.capital_scope(symbol), now_ms=NOW)
        assert isinstance(read, CapitalAvailable), read
        return read.basis_token

    async def attempt(
        self, session: AsyncSession, amount: str, *, symbol: str = "fUST"
    ) -> CommandAttempt:
        """A submit for ``amount`` on the policy applied now, with its audit decision row."""
        read = await self.capital.read(self.capital_scope(symbol), now_ms=NOW)
        assert isinstance(read, CapitalAvailable), read
        decision_id, correlation = str(uuid4()), uuid4()
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment=ENVIRONMENT, reconcile_id="contract", cell_id=CELL,
            symbol=symbol, signal_correlation_id=str(correlation), outcome="submitted",
            signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
            amount_usdt=Decimal(amount), duration_days=2, model_evidence={}, safety_result={},
            execution_policy="contract", service_version="test", config_hash="test",
            occurred_at_ms=NOW, recorded_at_ms=NOW,
        ))
        await session.flush()
        return CommandAttempt(
            uuid4(), decision_id, symbol,
            {"symbol": symbol, "amount": amount, "rate": "0.0001", "period": 2},
            Decimal(amount), NOW, read.applied.revision, read.applied.digest,
            read.applied.revision_id, command_date=date(2026, 10, 3), cell_id=CELL,
        )

    async def written(self) -> tuple[int, int]:
        """What a refused or aborted authorize must leave unchanged."""
        return await self.builders.written()


class _LedgerBuilders:
    def __init__(self, factory: async_sessionmaker[Any]) -> None:
        self.book = Book(factory, SCOPE)
        self.factory = factory

    async def policy(self, symbol: str, reserve: str, enabled: bool) -> None:
        await self.book.policy(symbol, reserve=reserve, enabled=enabled)

    async def snapshot(self, available: str, offers: tuple[Venue, ...]) -> None:
        observation = _observation(
            available,
            usd=True,  # the wallet reports fUSD (0) too
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

    async def written(self) -> tuple[int, int]:
        async with self.factory() as session:
            return (
                await session.scalar(select(CapitalCommandClockRow.revision)) or 0,
                await session.scalar(
                    select(func.count()).select_from(SubmissionAttemptJournalRow)
                ) or 0,
            )

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


def build_stack(factory: async_sessionmaker[Any]) -> Stack:
    return Stack(
        "ledger", factory,
        build_capital_authority(factory, max_snapshot_age_ms=MAX_SNAPSHOT_AGE_MS),
        build_uncertainty_reader(factory), build_managed_offer_reader(), build_scope_lock(),
        build_command_journal(factory, max_snapshot_age_ms=MAX_SNAPSHOT_AGE_MS),
        _LedgerBuilders(factory),
    )
