"""The capital authority every process runs under, and the ledger's boot guard for the seed.

The ledger is the only capital authority (S1-8). ``capital_authority_epoch`` still names
it, and every process (the bot on either venue, the web API, the owner's policy script)
refuses a database whose latest epoch is not ``ledger`` (``core.authority``): a database
that was never switched, or a backup restored from before the switch, is not one this build
may run on. A database without legacy history starts on the ledger at migration
(``b1c2d3e4f5a6``); one with legacy history only through the S1-7 switch.

A scope with legacy history must also hold the seed (H-1, 2026-10-05): the bot on the real
venue refuses to boot until such a scope has its ``legacy_seed`` observation
(``require_ledger_seed``). Without it the first runtime basis is a ``baseline`` that
disowns every resting offer and open credit the legacy authority placed, and the seed could
never run afterwards (it refuses a non-empty ledger). A scope without legacy history (a
fresh host) needs no seed: whatever rests at the venue is foreign to the ledger, which is
what it is. The simulated venue never needs one: its offers live in its own log, never in
the legacy history.
"""
from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.authority import Authority, AuthorityMismatch
from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow

SUPPORTED: Final[frozenset[Authority]] = frozenset({"ledger"})


async def has_legacy_history(session: AsyncSession, scope: Scope) -> bool:
    """Whether the frozen ``event_log`` holds a row of ``scope`` (``exchange_account_id`` is
    NOT NULL in the database since ``9b2c3d4e5f6a``)."""
    found = await session.scalar(
        select(EventLogRow.event_seq).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
        ).limit(1)
    )
    return found is not None


async def require_ledger_seed(
    session: AsyncSession, *, venue: Venue, scopes: tuple[Scope, ...]
) -> None:
    """Refuse (AuthorityMismatch) a real-venue boot while any of ``scopes`` has legacy
    history but no ``legacy_seed`` observation; every other boot passes."""
    if venue == "simulated":
        return
    for scope in scopes:
        if not await has_legacy_history(session, scope):
            continue
        seeded = await session.scalar(
            select(LedgerObservationRow.id).where(
                LedgerObservationRow.exchange_account_id == scope.exchange_account_id,
                LedgerObservationRow.deployment_environment == scope.deployment_environment,
                LedgerObservationRow.origin == "legacy_seed",
            ).limit(1)
        )
        if seeded is None:
            raise AuthorityMismatch(
                f"ledger_seed_missing scope={scope.exchange_account_id}:"
                f"{scope.deployment_environment}"
            )
