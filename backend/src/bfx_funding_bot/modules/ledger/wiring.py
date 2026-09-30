"""Construct the dormant ledger ports; apps select them in S1-3."""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Acceptance,
    Attempt,
    Authorized,
    AuthorizeRefused,
    CancelProvenance,
    LedgerCapitalRead,
    LedgerCapitalReader,
    LedgerJournal,
    LedgerManagedOffers,
    LedgerObservations,
    LedgerUncertainties,
    ManagedOffers,
    Observation,
    OpenUncertainty,
    Outcome,
    Quarantine,
    QuarantineMember,
    QueryHandle,
    Resolution,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import (
    capital_reader,
    clock,
    journal,
    observation,
    quarantine,
    reads,
)
from bfx_funding_bot.modules.trading import CapitalScope


class _SqlLedgerJournal:
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int:
        return await clock.bump_clock(session, scope)

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle:
        return await clock.begin_query(session, scope, started_at_ms)

    async def authorize_attempt(
        self,
        session: AsyncSession,
        scope: Scope,
        attempt: Attempt,
        basis_token: str,
        *,
        now_ms: int,
    ) -> Authorized | AuthorizeRefused:
        return await journal.authorize_attempt(session, scope, attempt, basis_token, now_ms=now_ms)

    async def close_dangling(
        self, session: AsyncSession, scope: Scope, *, now_ms: int, grace_ms: int = 120_000
    ) -> tuple[UUID, ...]:
        return await journal.close_dangling(session, scope, now_ms=now_ms, grace_ms=grace_ms)

    async def record_outcome(self, session: AsyncSession, scope: Scope, outcome: Outcome) -> None:
        await journal.record_outcome(session, scope, outcome)

    async def read_back_outcome(self, session: AsyncSession, attempt_id: UUID) -> Outcome | None:
        return await journal.read_back_outcome(session, attempt_id)

    async def record_resolution(
        self, session: AsyncSession, scope: Scope, resolution: Resolution
    ) -> None:
        await journal.record_resolution(session, scope, resolution)

    async def open_quarantine(
        self, session: AsyncSession, scope: Scope, opening: Quarantine
    ) -> None:
        await quarantine.open_quarantine(session, scope, opening)

    async def add_quarantine_member(
        self, session: AsyncSession, scope: Scope, member: QuarantineMember
    ) -> None:
        await quarantine.add_quarantine_member(session, scope, member)


def build_ledger_journal() -> LedgerJournal:
    return _SqlLedgerJournal()


class _SqlLedgerCapitalReader:
    async def read_capital(
        self,
        session: AsyncSession,
        scope: CapitalScope,
        *,
        now_ms: int,
        max_snapshot_age_ms: int,
    ) -> LedgerCapitalRead:
        return await capital_reader.read_capital(
            session, scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms
        )


def build_ledger_capital_reader() -> LedgerCapitalReader:
    return _SqlLedgerCapitalReader()


class _SqlLedgerObservations:
    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle:
        return await clock.begin_query(session, scope, started_at_ms)

    async def accept(
        self,
        session: AsyncSession,
        scope: Scope,
        handle: QueryHandle,
        first: Observation,
        confirmation: Observation,
        confirmation_started_at_ms: int,
    ) -> Acceptance:
        return await observation.accept_observation(
            session, scope, handle, first, confirmation, confirmation_started_at_ms
        )


def build_ledger_observations() -> LedgerObservations:
    return _SqlLedgerObservations()


class _SqlLedgerUncertainties:
    async def open_uncertainties(
        self, session: AsyncSession, scope: Scope, symbol: str | None = None
    ) -> tuple[OpenUncertainty, ...]:
        return await reads.open_uncertainties(session, scope, symbol)


def build_ledger_uncertainties() -> LedgerUncertainties:
    return _SqlLedgerUncertainties()


class _SqlLedgerManagedOffers:
    async def managed_live_offers(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> ManagedOffers:
        return await reads.managed_live_offers(session, scope, symbols)

    async def cancel_provenance(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str
    ) -> CancelProvenance | None:
        return await reads.cancel_provenance(session, scope, venue_offer_id)

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[Decimal]:
        return await reads.fingerprints_in_use(session, scope, symbol)


def build_ledger_managed_offers() -> LedgerManagedOffers:
    return _SqlLedgerManagedOffers()
