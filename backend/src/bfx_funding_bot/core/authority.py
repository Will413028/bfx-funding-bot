"""The capital authority a database is under, read once at boot.

``capital_authority_epoch`` is insert-only; its latest row by ``epoch_seq`` names the
authority. ``ledger`` is the only one a build since S1-8 runs on, so every process (the bot on
either venue, the web API, the owner's policy script, the restore drill's boot check) refuses
any other (``require_ledger_authority``). ``legacy`` is still a value the table holds (the
seed row, a database restored from before the switch), so it is named to be refused. Only the
owner appends a row: the S1-7 switch, or the genesis migration on a database without legacy
history (``apps/authority_support`` checks which, for the bot on the real venue).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

AUTHORITY_TABLE: Final = "capital_authority_epoch"
# The values the epoch table admits (``ck_capital_authority_epoch_authority``).
_KNOWN: Final[frozenset[str]] = frozenset({"legacy", "ledger"})


class AuthorityMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database's capital authority is missing, unknown or not the ledger."""


async def _latest_epoch(session: AsyncSession) -> tuple[str, str]:
    """The latest epoch's ``(authority, actor)``; a missing table or row refuses."""
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table(AUTHORITY_TABLE)
    )
    if not exists:
        raise AuthorityMismatch("authority_missing table=capital_authority_epoch")
    row = (await session.execute(text(
        "SELECT authority, actor FROM capital_authority_epoch "
        "ORDER BY epoch_seq DESC LIMIT 1"))).first()
    if row is None:
        raise AuthorityMismatch("authority_missing row=none")
    return row.authority, str(row.actor)


def _require_ledger(value: object) -> None:
    if not isinstance(value, str) or value not in _KNOWN:
        raise AuthorityMismatch(f"authority_unknown value={value!r}")
    if value != "ledger":
        raise AuthorityMismatch(f"authority_unsupported value={value} build=ledger")


async def require_ledger_authority(session: AsyncSession) -> str:
    """Refuse (AuthorityMismatch) unless the latest epoch is ``ledger``; return its writer
    (the epoch's ``actor``), read in the same statement."""
    authority, actor = await _latest_epoch(session)
    _require_ledger(authority)
    return actor


__all__ = [
    "AUTHORITY_TABLE",
    "AuthorityMismatch",
    "require_ledger_authority",
]
