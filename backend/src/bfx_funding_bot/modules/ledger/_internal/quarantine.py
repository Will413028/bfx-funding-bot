"""Quarantine openings and immutable memberships."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import (
    Quarantine,
    QuarantineMember,
    QuarantineMemberConflict,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal.clock import bump_locked, lock_scope
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    ExecutionResolutionJournalRow,
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
            source.deployment_environment,
            source.symbol,
        ) != (scope.exchange_account_id, scope.deployment_environment, quarantine.symbol):
            raise ValueError("quarantine source attempt scope mismatch")
    revision = await bump_locked(session, scope)
    session.add(
        QuarantineOpeningRow(
            quarantine_id=quarantine.quarantine_id,
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
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
    if (opening.exchange_account_id, opening.deployment_environment) != (
        scope.exchange_account_id,
        scope.deployment_environment,
    ):
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


async def has_unresolved_quarantine(
    session: AsyncSession, scope: Scope, basis: AcceptedCapitalBasisRow | None, symbol: str
) -> bool:
    """``unresolved_quarantines`` for one symbol as one bounded EXISTS (LIMIT 1)."""
    candidate = QuarantineOpeningRow.opened_revision > (
        basis.accept_revision if basis is not None else 0
    )
    if basis is not None:
        candidate = candidate | QuarantineOpeningRow.quarantine_id.in_(
            select(AcceptedCapitalBasisQuarantineRow.quarantine_id).where(
                AcceptedCapitalBasisQuarantineRow.basis_id == basis.id
            )
        )
    found = await session.scalar(
        select(QuarantineOpeningRow.quarantine_id)
        .where(
            QuarantineOpeningRow.exchange_account_id == scope.exchange_account_id,
            QuarantineOpeningRow.deployment_environment == scope.deployment_environment,
            QuarantineOpeningRow.symbol == symbol,
            candidate,
            ~exists().where(
                ExecutionResolutionJournalRow.quarantine_id == QuarantineOpeningRow.quarantine_id
            ),
        )
        .limit(1)
    )
    return found is not None


async def unresolved_quarantines(
    session: AsyncSession,
    scope: Scope,
    basis: AcceptedCapitalBasisRow | None,
    *,
    symbol: str | None = None,
) -> list[QuarantineOpeningRow]:
    """Quarantines of the scope without a resolution, ordered by opening.

    Bounded by ``basis`` (the latest accepted one): an opening at or before its
    ``accept_revision`` that it did not list was resolved by then, and a
    resolution is permanent. So the candidates are the basis's listed
    quarantines plus openings with ``opened_revision > accept_revision``;
    without a basis, every opening of the scope.
    """
    accept_revision = basis.accept_revision if basis is not None else 0
    # Explicit columns: the web API reads this table under a column allowlist
    # that excludes ``evidence``. Callers use only these.
    columns = load_only(
        QuarantineOpeningRow.quarantine_id,
        QuarantineOpeningRow.symbol,
        QuarantineOpeningRow.intended_amount,
        QuarantineOpeningRow.opened_at_ms,
        QuarantineOpeningRow.source_attempt_id,
    )
    since = select(QuarantineOpeningRow).options(columns).where(
        QuarantineOpeningRow.exchange_account_id == scope.exchange_account_id,
        QuarantineOpeningRow.deployment_environment == scope.deployment_environment,
        QuarantineOpeningRow.opened_revision > accept_revision,
    )
    if symbol is not None:
        since = since.where(QuarantineOpeningRow.symbol == symbol)
    candidates = {row.quarantine_id: row for row in await session.scalars(since)}
    if basis is not None:
        listed = (
            select(QuarantineOpeningRow)
            .options(columns)
            .join(
                AcceptedCapitalBasisQuarantineRow,
                AcceptedCapitalBasisQuarantineRow.quarantine_id
                == QuarantineOpeningRow.quarantine_id,
            )
            .where(AcceptedCapitalBasisQuarantineRow.basis_id == basis.id)
        )
        if symbol is not None:
            listed = listed.where(QuarantineOpeningRow.symbol == symbol)
        for row in await session.scalars(listed):
            candidates[row.quarantine_id] = row
    if not candidates:
        return []
    resolved: set[UUID | None] = set(
        await session.scalars(
            select(ExecutionResolutionJournalRow.quarantine_id).where(
                ExecutionResolutionJournalRow.quarantine_id.in_(sorted(candidates))
            )
        )
    )
    return sorted(
        (row for quarantine_id, row in candidates.items() if quarantine_id not in resolved),
        key=lambda row: (row.opened_at_ms, row.quarantine_id),
    )
