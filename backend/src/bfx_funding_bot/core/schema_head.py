"""The one database schema this build runs on, checked when the live daemon boots.

Hand-maintained on purpose -- adopting a schema is a decision, not a side
effect of a file appearing -- and pinned by ``tests/test_schema_head.py``, which
fails in a second when a migration lands without moving it.

The deploy tool migrates before it starts a build, so a live daemon that finds
another head is the wrong build for this database (for instance a rollback onto
a newer schema, which the old code cannot run on). Before anything can trade,
the daemon then records ``HALTED`` (cause ``auto``, which alerts the operator)
and refuses to boot.
"""
from __future__ import annotations

from typing import Final

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

SCHEMA_HEAD: Final = "9391a0f675d3"


class SchemaHeadMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database is not at the schema this build was written for."""


async def database_heads(session: AsyncSession) -> tuple[str, ...]:
    """The database's alembic heads; empty when it was never migrated."""
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table("alembic_version"))
    if not exists:
        return ()
    return tuple(sorted(await session.scalars(text("SELECT version_num FROM alembic_version"))))


async def assert_schema_head(session: AsyncSession) -> None:
    heads = await database_heads(session)
    if heads != (SCHEMA_HEAD,):
        raise SchemaHeadMismatch(
            f"schema_head_mismatch database={','.join(heads) or 'none'} build={SCHEMA_HEAD}")


__all__ = ["SCHEMA_HEAD", "SchemaHeadMismatch", "assert_schema_head", "database_heads"]
