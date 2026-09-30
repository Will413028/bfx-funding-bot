"""Construct the dormant ledger ports; apps select them in S1-3."""

from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.ledger import (
    Acceptance,
    Attempt,
    Authorized,
    AuthorizeRefused,
    CancelAdmitted,
    CancelProvenance,
    CommandAttempt,
    CommandJournal,
    CommandOutcome,
    CommandRefused,
    LedgerCapitalRead,
    LedgerCapitalReader,
    LedgerJournal,
    LedgerManagedOffers,
    LedgerObservations,
    LedgerUncertainties,
    LockedCancelGuard,
    LockedCommandGuard,
    ManagedOffers,
    Observation,
    OpenUncertainty,
    Outcome,
    OutcomeAlreadyRecorded,
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
from bfx_funding_bot.modules.ledger.tables import SubmissionAttemptJournalRow
from bfx_funding_bot.modules.trading import CapitalScope


class _SqlCommandJournal:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def authorize(
        self, session: AsyncSession, scope: Scope, attempt: CommandAttempt,
        basis_token: str, *, now_ms: int, locked_guard: LockedCommandGuard,
    ) -> Authorized | CommandRefused:
        return await journal.authorize_command(
            session, scope, attempt, basis_token, now_ms=now_ms, locked_guard=locked_guard,
        )

    async def admit_cancel(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str,
        *, now_ms: int, locked_guard: LockedCancelGuard,
    ) -> CancelAdmitted | CommandRefused:
        return await journal.admit_cancel(
            session, scope, venue_offer_id, now_ms=now_ms, locked_guard=locked_guard,
        )

    @staticmethod
    def _command_outcome(stored: Outcome) -> CommandOutcome:
        return CommandOutcome(stored.kind, stored.venue_offer_id, stored.reason,
                              stored.completed_at_ms, stored.evidence)

    async def record_outcome(self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome) -> None:
        async with self._session_factory.begin() as session:
            try:
                await journal.record_outcome(session, scope, Outcome(
                    attempt_id, outcome.kind, outcome.venue_offer_id, outcome.reason,
                    outcome.completed_at_ms, outcome.evidence,
                ))
            except OutcomeAlreadyRecorded as exc:
                assert isinstance(exc.stored, Outcome)
                raise OutcomeAlreadyRecorded(self._command_outcome(exc.stored)) from exc

    async def read_back_outcome(self, scope: Scope, attempt_id: UUID) -> CommandOutcome | None:
        async with self._session_factory() as session:
            attempt = await session.get(SubmissionAttemptJournalRow, attempt_id)
            if attempt is None:
                raise ValueError("attempt does not exist")
            if (attempt.exchange_account_id, attempt.deployment_environment) != (
                scope.exchange_account_id, scope.deployment_environment,
            ):
                raise ValueError("attempt scope mismatch")
            stored = await journal.read_back_outcome(session, attempt_id)
            if stored is not None and stored.attempt_id != attempt_id:
                raise ValueError("outcome identity mismatch")
            return None if stored is None else self._command_outcome(stored)


def build_command_journal(session_factory: async_sessionmaker[AsyncSession]) -> CommandJournal:
    return _SqlCommandJournal(session_factory)


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
