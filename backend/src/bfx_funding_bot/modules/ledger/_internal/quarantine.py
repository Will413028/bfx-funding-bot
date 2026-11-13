"""Quarantine openings and immutable memberships."""

from __future__ import annotations

from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Quarantine,
    QuarantineMember,
    QuarantineMemberConflict,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal.clock import bump_locked, lock_scope
from bfx_funding_bot.modules.ledger.tables import (
    QuarantineMemberRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
)


async def open_quarantine(session: AsyncSession, scope: Scope, quarantine: Quarantine) -> None:
    """Bump the clock first so the opening carries the revision it created.

    ``opened_revision > basis.accept_revision`` then names exactly the
    quarantines a basis has not seen (readers need no scope-wide anti-join).
    """
    await lock_scope(session, scope)
    if quarantine.source_attempt_id is not None:
        source = await session.get(SubmissionAttemptJournalRow, quarantine.source_attempt_id)
        if source is None or (
            source.exchange_account_id,
            source.symbol,
        ) != (scope.exchange_account_id, quarantine.symbol):
            raise ValueError("quarantine source attempt scope mismatch")
    revision = await bump_locked(session, scope)
    session.add(
        QuarantineOpeningRow(
            quarantine_id=quarantine.quarantine_id,
            exchange_account_id=scope.exchange_account_id,
            symbol=quarantine.symbol,
            intended_amount=quarantine.intended_amount,
            opened_at_ms=quarantine.opened_at_ms,
            opened_revision=revision,
            evidence=quarantine.evidence,
            legacy_reconcile_event_seq=quarantine.legacy_reconcile_event_seq,
            source_attempt_id=quarantine.source_attempt_id,
        )
    )
    await session.flush()


async def add_quarantine_member(
    session: AsyncSession, scope: Scope, member: QuarantineMember
) -> None:
    await lock_scope(session, scope)
    opening = await session.get(QuarantineOpeningRow, member.quarantine_id)
    if opening is None:
        raise ValueError("quarantine does not exist")
    if opening.exchange_account_id != scope.exchange_account_id:
        raise ValueError("quarantine scope mismatch")
    existing = await session.get(
        QuarantineMemberRow, (member.quarantine_id, member.source_kind, member.venue_object_id)
    )
    if existing is not None:
        raise QuarantineMemberConflict(
            QuarantineMember(
                existing.quarantine_id,
                cast(Literal["offer", "credit", "loan"], existing.source_kind),
                existing.venue_object_id,
                existing.observation_id,
                existing.amount_at_join,
            )
        )
    session.add(
        QuarantineMemberRow(
            quarantine_id=member.quarantine_id,
            source_kind=member.source_kind,
            venue_object_id=member.venue_object_id,
            observation_id=member.observation_id,
            amount_at_join=member.amount_at_join,
        )
    )
    await session.flush()
    await bump_locked(session, scope)
