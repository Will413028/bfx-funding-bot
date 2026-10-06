"""The capital authority a database is under, read once at boot.

``capital_authority_epoch`` is insert-only; its latest row by ``epoch_seq`` names the
authority. ``ledger`` is the only one a build since S1-8 runs on; ``legacy`` is still a value
the table holds (the seed row, a database restored from before the switch), so it must be
named to be refused. Only the owner appends a row -- the S1-7 switch, or the genesis
migration on a database without legacy history -- and every process refuses an authority
outside the set it passes (``apps/authority_support.SUPPORTED``).
"""

from __future__ import annotations

from typing import Final, Literal, cast, get_args

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

Authority = Literal["legacy", "ledger"]

AUTHORITY_TABLE: Final = "capital_authority_epoch"
KNOWN_AUTHORITIES: Final[frozenset[str]] = frozenset(get_args(Authority))


class AuthorityMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database's capital authority is missing, unknown or not supported here."""


def check_authority(value: object, *, supported: frozenset[Authority]) -> Authority:
    """The authority ``value`` names if it is one of ``supported``, or AuthorityMismatch."""
    if not isinstance(value, str) or value not in KNOWN_AUTHORITIES:
        raise AuthorityMismatch(f"authority_unknown value={value!r}")
    authority = cast(Authority, value)
    if authority not in supported:
        raise AuthorityMismatch(
            f"authority_unsupported value={authority} build={','.join(sorted(supported))}"
        )
    return authority


async def read_authority(session: AsyncSession, *, supported: frozenset[Authority]) -> Authority:
    """The latest epoch's authority; missing table or row, unknown or unsupported refuse.

    ``supported`` is the caller's: each process (a venue, the web API, an owner script)
    names the authorities it can run under; there is no build-wide set.
    """
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table(AUTHORITY_TABLE)
    )
    if not exists:
        raise AuthorityMismatch("authority_missing table=capital_authority_epoch")
    latest = text("SELECT authority FROM capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1")
    rows = (await session.execute(latest)).scalars().all()
    if not rows:
        raise AuthorityMismatch("authority_missing row=none")
    return check_authority(rows[0], supported=supported)


__all__ = [
    "AUTHORITY_TABLE",
    "KNOWN_AUTHORITIES",
    "Authority",
    "AuthorityMismatch",
    "check_authority",
    "read_authority",
]
