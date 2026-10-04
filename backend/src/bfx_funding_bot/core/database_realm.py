"""The realm a database is stamped with, read at boot.

``database_realm`` is a one-row, insert-only table (ADR 2026-10-03 D1/E2). A trigger on
every realm table rejects a write whose ``deployment_environment`` differs from the stamp,
and every write while the database is unstamped. A process refuses to boot on a database
whose stamp is missing or differs from the realm it runs as, so a ``live`` process under
realm ``ci`` can no longer write ``ci`` rows into the production database.

Only the owner writes the stamp, once: the migration derives it from existing data, a fresh
host or a simulation database is stamped explicitly (docs/runbooks/fresh-host-setup.md).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Text, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

DATABASE_REALM_TABLE: Final = "database_realm"
KNOWN_REALMS: Final[tuple[str, ...]] = ("prod", "shadow", "ci")


class DatabaseRealmRow(Base):
    """The one stamp row; the migration owns its triggers, grants and the seed."""

    __tablename__ = DATABASE_REALM_TABLE
    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text("true"))
    realm: Mapped[str] = mapped_column(Text, nullable=False)
    stamped_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("id", name="ck_database_realm_singleton"),
        CheckConstraint("realm IN ('prod', 'shadow', 'ci')", name="ck_database_realm_realm"),
        CheckConstraint("actor <> ''", name="ck_database_realm_actor"),
    )


class DatabaseRealmMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database is unstamped, or stamped with a realm this process does not run as."""


async def read_database_realm(session: AsyncSession) -> str:
    """The stamped realm; a missing table, a missing row or an unknown value refuses."""
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table(DATABASE_REALM_TABLE)
    )
    if not exists:
        raise DatabaseRealmMismatch("database_realm_missing table=database_realm")
    rows = (await session.execute(text("SELECT realm FROM database_realm"))).scalars().all()
    if not rows:
        raise DatabaseRealmMismatch("database_realm_unstamped row=none")
    realm = rows[0]
    if not isinstance(realm, str) or realm not in KNOWN_REALMS:
        raise DatabaseRealmMismatch(f"database_realm_unknown value={realm!r}")
    return realm


async def assert_database_realm(session: AsyncSession, expected: str) -> str:
    """Return the stamp when it equals ``expected``; refuse otherwise."""
    if expected not in KNOWN_REALMS:
        raise DatabaseRealmMismatch(f"database_realm_expected_unknown value={expected!r}")
    stamped = await read_database_realm(session)
    if stamped != expected:
        raise DatabaseRealmMismatch(f"database_realm_mismatch stamp={stamped} process={expected}")
    return stamped


__all__ = [
    "DATABASE_REALM_TABLE",
    "KNOWN_REALMS",
    "DatabaseRealmMismatch",
    "DatabaseRealmRow",
    "assert_database_realm",
    "read_database_realm",
]
