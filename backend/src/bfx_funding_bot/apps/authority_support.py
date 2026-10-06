"""The bot's boot guard on the capital authority epoch: the ledger, written by a known writer.

The ledger is the only capital authority (S1-8): every process refuses a database whose latest
epoch is not ``ledger`` (``core.authority.require_ledger_authority``). The bot on the real
venue also requires that epoch to come from one of the two owners that ever append it, and
refuses any other writer (fail closed):

* the S1-7 switch (actor ``ledger_seed:<run>-a<n>``; its tool is deleted, prod's epoch of
  2026-10-05 stays): it wrote the seed and the epoch in one transaction, and the seed rows
  are append-only ledger data, so the epoch alone says the seed is there;
* the genesis migration (``b1c2d3e4f5a6``), which appends its epoch only on a database whose
  legacy ``event_log`` is empty: nothing was ever lent under the legacy authority, so nothing
  needs a seed, and whatever rests at the venue is foreign to the ledger.

No scope is checked: a scope's seed exists only for the scopes the switch saw, and the seed can
no longer run, so a per-scope check could only refuse an account added after the switch. The
simulated venue is exempt (its offers live in its own log; ``scripts/bootstrap_simulation_db``
appends its own epoch).
"""
from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.authority import AuthorityMismatch, require_ledger_authority
from bfx_funding_bot.core.venue import Venue

# The epoch writers this build knows (tests pin both): the S1-7 switch's actor prefix as it
# wrote it in prod, and the genesis migration's ``ACTOR``.
SWITCH_ACTOR_PREFIX: Final = "ledger_seed:"
GENESIS_ACTOR: Final = "migration b1c2d3e4f5a6 genesis"


async def require_ledger_epoch(session: AsyncSession, *, venue: Venue) -> None:
    """Refuse (AuthorityMismatch) unless the latest epoch is ``ledger`` and, on the real venue,
    was written by the genesis migration or the S1-7 switch."""
    actor = await require_ledger_authority(session)
    if venue == "simulated":
        return
    if actor != GENESIS_ACTOR and not actor.startswith(SWITCH_ACTOR_PREFIX):
        raise AuthorityMismatch(f"ledger_epoch_writer_unknown actor={actor!r}")
