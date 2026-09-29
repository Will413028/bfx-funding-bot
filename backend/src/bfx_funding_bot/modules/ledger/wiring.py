"""Construct the dormant ledger journal port; apps select it in S1-3."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Attempt,
    LedgerJournal,
    Outcome,
    Quarantine,
    QuarantineMember,
    QueryHandle,
    RecordedAttempt,
    Resolution,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import clock, journal, quarantine


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
