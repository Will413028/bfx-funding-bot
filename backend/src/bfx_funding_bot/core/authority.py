"""The capital authority this build runs under, read once at boot.

``capital_authority_epoch`` is insert-only; its latest row by ``epoch_seq`` names
the authority (``legacy`` or ``ledger``). Only the owner appends a row -- the
S1-7 switch -- and a build refuses to run on an authority it does not support,
so a switched database never meets a build that would write the other one.
"""

from __future__ import annotations

from typing import Final, Literal, cast, get_args

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

Authority = Literal["legacy", "ledger"]

AUTHORITY_TABLE: Final = "capital_authority_epoch"
KNOWN_AUTHORITIES: Final[frozenset[str]] = frozenset(get_args(Authority))
# This build still wires every capital path to the legacy authority.
SUPPORTED_AUTHORITIES: Final[frozenset[Authority]] = frozenset({"legacy"})


class AuthorityMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database's capital authority is missing, unknown or not supported here."""


def check_authority(value: object) -> Authority:
    """The supported authority ``value`` names, or AuthorityMismatch."""
    if not isinstance(value, str) or value not in KNOWN_AUTHORITIES:
        raise AuthorityMismatch(f"authority_unknown value={value!r}")
    authority = cast(Authority, value)
    if authority not in SUPPORTED_AUTHORITIES:
        supported = ",".join(sorted(SUPPORTED_AUTHORITIES))
        raise AuthorityMismatch(f"authority_unsupported value={authority} build={supported}")
    return authority


async def read_authority(session: AsyncSession) -> Authority:
    """The latest epoch's authority; missing table or row, unknown or unsupported refuse."""
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table(AUTHORITY_TABLE)
    )
    if not exists:
        raise AuthorityMismatch("authority_missing table=capital_authority_epoch")
    latest = text("SELECT authority FROM capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1")
    rows = (await session.execute(latest)).scalars().all()
    if not rows:
        raise AuthorityMismatch("authority_missing row=none")
    return check_authority(rows[0])


__all__ = [
    "AUTHORITY_TABLE",
    "KNOWN_AUTHORITIES",
    "SUPPORTED_AUTHORITIES",
    "Authority",
    "AuthorityMismatch",
    "check_authority",
    "read_authority",
]
