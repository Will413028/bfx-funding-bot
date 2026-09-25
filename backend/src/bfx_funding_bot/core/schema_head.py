"""The schema this build runs on, derived from the migrations shipped in its image.

Nothing here is hand-maintained. The image carries ``alembic/``; its single
head is the schema the build was written for, and the live daemon refuses to
boot (HALTED/auto first) on a database at any other revision -- the deploy tool
migrates before it starts a build, so another head means the wrong build for
this database, for instance a rollback onto a newer schema. More than one head
in the image is refused the same way.

Two contracts span several revisions: the serialized projector's seeded cursor
and the projection archive. Each was established by one migration; every later
migration either preserves it or changes it, and says which in a module
attribute ``ledger_contract = "preserved" | "changed"``. The revisions a
contract holds on are then derived -- from the head back to the newest
migration that changed it (or to the one that established it) -- and
``tests/test_schema_head.py`` refuses a migration that does not declare.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

ALEMBIC_DIR: Final = Path(__file__).resolve().parents[3] / "alembic"
LEDGER_CONTRACT: Final = "ledger_contract"
# The migrations that established each contract.
PROJECTOR_CONTRACT_BASE: Final = "c2e3f4a5b6c7"  # seeds the serialized-projector cursors
ARCHIVE_CONTRACT_BASE: Final = "f8c2d4e6a901"    # adds the projection_audit archive
# Revisions up to here predate the declaration and are known to preserve both.
DECLARED_AFTER: Final = "a7f3c1d9e204"


class SchemaHeadMismatch(RuntimeError):  # noqa: N818 - a refused boot state
    """The database is not at the schema this build was written for."""


@cache
def migration_scripts() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(config)


def build_head() -> str:
    """The single head of this build's migrations; several are refused."""
    heads = tuple(migration_scripts().get_heads())
    if len(heads) != 1:
        raise SchemaHeadMismatch(f"schema_heads_ambiguous build={','.join(heads) or 'none'}")
    return heads[0]


def declared_contract(revision: str) -> str | None:
    module = migration_scripts().get_revision(revision).module
    value = getattr(module, LEDGER_CONTRACT, None)
    return value if isinstance(value, str) else None


@cache
def contract_revisions(base: str) -> frozenset[str]:
    """Revisions the contract established at ``base`` holds on."""
    ready: list[str] = []
    # Head down to (not including) base; the base itself established the contract.
    for script in migration_scripts().iterate_revisions(build_head(), base):
        if script.revision == base:
            break
        ready.append(script.revision)
        if declared_contract(script.revision) == "changed":
            return frozenset(ready)  # older revisions predate its current shape
    ready.append(base)
    return frozenset(ready)


async def database_heads(session: AsyncSession) -> tuple[str, ...]:
    """The database's alembic heads; empty when it was never migrated."""
    exists = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table("alembic_version"))
    if not exists:
        return ()
    return tuple(sorted(await session.scalars(text("SELECT version_num FROM alembic_version"))))


async def assert_schema_head(session: AsyncSession) -> None:
    expected = build_head()
    heads = await database_heads(session)
    if heads != (expected,):
        raise SchemaHeadMismatch(
            f"schema_head_mismatch database={','.join(heads) or 'none'} build={expected}")


__all__ = [
    "ARCHIVE_CONTRACT_BASE",
    "DECLARED_AFTER",
    "LEDGER_CONTRACT",
    "PROJECTOR_CONTRACT_BASE",
    "SchemaHeadMismatch",
    "assert_schema_head",
    "build_head",
    "contract_revisions",
    "database_heads",
    "declared_contract",
    "migration_scripts",
]
