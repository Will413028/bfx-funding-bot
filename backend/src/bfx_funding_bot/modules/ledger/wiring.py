"""Construct the dormant ledger ports; apps select them in S1-3."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Attempt,
    LedgerCapitalRead,
    LedgerCapitalReader,
    LedgerJournal,
    Outcome,
    Quarantine,
    QuarantineMember,
    QueryHandle,
    RecordedAttempt,
    Resolution,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader, clock, journal, quarantine
from bfx_funding_bot.modules.trading import CapitalScope


class _SqlLedgerJournal:
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int:
        return await clock.bump_clock(session, scope)

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle:
        return await clock.begin_query(session, scope, started_at_ms)

    async def record_attempt(
        self, session: AsyncSession, scope: Scope, attempt: Attempt
    ) -> RecordedAttempt:
        return await journal.record_attempt(session, scope, attempt)

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
