"""The capital authority every process runs under, and the ledger's boot guard for the seed.

The ledger is the only capital authority (S1-8). ``capital_authority_epoch`` still names
it, and every process (the bot on either venue, the web API, the owner's policy script)
refuses a database whose latest epoch is not ``ledger`` (``core.authority``): a database
that was never switched, or a backup restored from before the switch, is not one this build
may run on.

How the latest ``ledger`` epoch came to be says whether the ledger needs the seed (H-1,
2026-10-05), so the guard reads only the epoch and the ledger, never the frozen legacy
tables:

* the S1-7 switch (actor ``ledger_seed:<run>-a<n>``) wrote the seed and the epoch in one
  transaction: every scope the bot runs must hold its ``legacy_seed`` observation. Without it
  the first runtime basis is a ``baseline`` that disowns every resting offer and open credit
  the legacy authority placed, and the seed could never run afterwards (it refuses a
  non-empty ledger);
* the genesis migration (``b1c2d3e4f5a6``) appends its epoch only on a database whose
  ``event_log`` is empty: nothing was ever lent under the legacy authority, so nothing needs
  a seed, and whatever rests at the venue is foreign to the ledger;
* any other writer is unknown to this build: the real venue refuses to boot (fail closed).

The simulated venue never needs the seed: its offers live in its own log, never in the
legacy history.
"""
from __future__ import annotations

from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.authority import Authority, AuthorityMismatch
from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow

SUPPORTED: Final[frozenset[Authority]] = frozenset({"ledger"})
# The epoch writers this build knows: the S1-7 switch (its tool, deleted in S1-8 PR-D, wrote
# ``ledger_seed:<run>-a<n>`` in prod on 2026-10-05) and the genesis migration's ``ACTOR``;
# tests pin both.
SWITCH_ACTOR_PREFIX: Final = "ledger_seed:"
GENESIS_ACTOR: Final = "migration b1c2d3e4f5a6 genesis"


async def _latest_epoch(session: AsyncSession) -> tuple[str, str]:
    row = (await session.execute(text(
        "SELECT authority, actor FROM capital_authority_epoch "
        "ORDER BY epoch_seq DESC LIMIT 1"))).first()
    if row is None:
        raise AuthorityMismatch("authority_missing row=none")
    return str(row.authority), str(row.actor)


async def require_ledger_seed(
    session: AsyncSession, *, venue: Venue, scopes: tuple[Scope, ...]
) -> None:
    """Refuse (AuthorityMismatch) a real-venue boot unless the latest ``ledger`` epoch was
    written by the genesis migration, or by the switch and every scope holds its seed."""
    if venue == "simulated":
        return
    authority, actor = await _latest_epoch(session)
    if authority != "ledger":
        raise AuthorityMismatch(f"authority_unsupported value={authority} build=ledger")
    if actor == GENESIS_ACTOR:
        return
    if not actor.startswith(SWITCH_ACTOR_PREFIX):
        raise AuthorityMismatch(f"ledger_epoch_writer_unknown actor={actor!r}")
    for scope in scopes:
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
