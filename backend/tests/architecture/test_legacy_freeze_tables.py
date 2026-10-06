"""Every table of the execution module is classified for the legacy freeze (``e8f9a0b1c2d3``),
and the frozen ones are exactly what ``c2d3e4f5a6b7`` archives.

The migration's lists are fixed (an applied migration never changes); this test makes a new
execution table fail until someone decides whether the switch freezes it (a later migration
adds it to the trigger) or it is shared. Mutation: move a table out of both lists, or add an
execution table without classifying it.
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path

import bfx_funding_bot.modules.execution as execution
from bfx_funding_bot.modules.execution import legacy_archive

_MIGRATION = (Path(__file__).resolve().parents[2] / "alembic/versions"
              / "e8f9a0b1c2d3_legacy_authority_freeze.py")
_ARCHIVE_MIGRATION = _MIGRATION.with_name("c2d3e4f5a6b7_legacy_archive_schema.py")


def _migration():
    spec = importlib.util.spec_from_file_location("legacy_authority_freeze", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execution_tables() -> set[str]:
    names: set[str] = set()
    for info in pkgutil.walk_packages(execution.__path__, prefix=f"{execution.__name__}."):
        if not info.name.rsplit(".", 1)[-1].endswith("tables"):
            continue
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            table = getattr(value, "__table__", None)
            if table is not None and getattr(value, "__module__", None) == module.__name__:
                names.add(table.fullname)
    return names


def test_every_execution_table_is_frozen_or_shared() -> None:
    migration = _migration()
    frozen, shared = set(migration.FROZEN), set(migration.SHARED)
    assert not frozen & shared
    # The frozen ones live in the archive since c2d3e4f5a6b7, owned by migrations: no ORM maps
    # them, so every mapped execution table is a shared one.
    assert _execution_tables() == shared


def test_the_archive_holds_exactly_the_frozen_tables() -> None:
    """The archive migration, the archive's schema module and the freeze name the same twelve."""
    spec = importlib.util.spec_from_file_location("legacy_archive_schema", _ARCHIVE_MIGRATION)
    assert spec is not None and spec.loader is not None
    archive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    frozen = set(_migration().FROZEN)
    assert set(archive.TABLES) == frozen == legacy_archive.TABLES
    assert archive.SCHEMA == legacy_archive.SCHEMA
