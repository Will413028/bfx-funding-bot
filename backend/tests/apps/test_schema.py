"""``apps/schema.py``: importing it registers a complete schema.

Mutations (one at a time; revert after each): drop ``ledger.tables`` from apps/schema.py (both
tests: other modules' foreign keys point at its tables); drop ``simulated_venue.tables``
(``test_every_table_module_is_in_the_schema``); drop the apps/schema.py import from
scripts/__init__.py (``test_the_scripts_package_registers_the_whole_schema``) or from
apps/operator_cli.py (its ``test_every_app_entrypoint_resolves...`` case).
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bfx_funding_bot.core.db import Base

_BACKEND = Path(__file__).resolve().parents[2]
_SRC = _BACKEND / "src"

_RESOLVE_ALONE = """
import bfx_funding_bot.apps.schema
from bfx_funding_bot.core.db import Base
from sqlalchemy.exc import NoReferencedTableError
for table in Base.metadata.tables.values():
    for foreign_key in table.foreign_keys:
        try:
            foreign_key.column
        except NoReferencedTableError:
            print(f"{table.name} -> {foreign_key.target_fullname}")
"""


def test_the_schema_alone_resolves_every_foreign_key() -> None:
    # A fresh interpreter: this process has whatever the other test modules imported.
    result = subprocess.run(
        [sys.executable, "-c", _RESOLVE_ALONE], capture_output=True, text=True, check=True,
    )
    assert result.stdout == ""


def test_every_table_module_is_in_the_schema() -> None:
    import bfx_funding_bot.apps.schema  # noqa: F401

    registered = set(Base.metadata.tables)
    for path in sorted(_SRC.rglob("*.py")):
        if "__tablename__" not in path.read_text():
            continue
        module = ".".join(path.relative_to(_SRC).with_suffix("").parts)
        importlib.import_module(module)
        assert set(Base.metadata.tables) == registered, f"{module} is missing from apps/schema.py"


_ENTRYPOINT_RESOLVES = """
import importlib, sys
importlib.import_module(sys.argv[1])
from bfx_funding_bot.core.db import Base
from sqlalchemy.exc import NoReferencedTableError
for table in Base.metadata.tables.values():
    for foreign_key in table.foreign_keys:
        try:
            foreign_key.column
        except NoReferencedTableError:
            print(f"{table.name} -> {foreign_key.target_fullname}")
print("tables", len(Base.metadata.tables))
"""
_MAIN_GUARD = re.compile(r"""__name__\s*==\s*["']__main__["']|["']__main__["']\s*==\s*__name__""")


def _resolve_alone(module: str) -> tuple[str, int]:
    """What a fresh interpreter that imports only `module` leaves unresolved, and how many
    tables it registered."""
    result = subprocess.run(
        [sys.executable, "-c", _ENTRYPOINT_RESOLVES, module],
        capture_output=True, text=True, check=True, cwd=_BACKEND,
        # This tree's sources first, not whatever checkout the editable install points at.
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(_SRC), str(_BACKEND)))},
    )
    *unresolved, tables = result.stdout.splitlines()
    return "".join(f"{line}\n" for line in unresolved), int(tables.split()[1])


def _app_entrypoints() -> list[str]:
    """Every apps module a process starts from (a `__main__` guard or a package `__main__`),
    and the web API (uvicorn imports it). Only what they import at load time is seen: a table
    module imported inside a function (apps/restore_boot_check.py's ledger.tables) is not."""
    apps = _SRC / "bfx_funding_bot" / "apps"
    found = {"bfx_funding_bot.apps.webapi"}
    for path in sorted(apps.rglob("*.py")):
        if path.name == "__main__.py" or _MAIN_GUARD.search(path.read_text(encoding="utf-8")):
            parts = path.relative_to(_SRC).with_suffix("").parts
            found.add(".".join(parts[:-1] if path.name == "__main__.py" else parts))
    return sorted(found)


# The control plane may not import the full schema (control-plane-exposure contract: it would
# reach the execution writers' tables). It loads this journal's table for reads only: bfx_webapi
# has no INSERT or UPDATE on it, so no flush sorts it, and the web API never asks the metadata
# for the relationship (no sorted_tables, create_all or FK-inferred join; rg in apps/webapi.py
# and modules/api).
_KNOWN_UNRESOLVED = {
    "bfx_funding_bot.apps.webapi": "submission_attempt_journal -> execution_decisions.decision_id\n",
}


@pytest.mark.parametrize("module", _app_entrypoints())
def test_every_app_entrypoint_resolves_every_foreign_key_it_loads(module: str) -> None:
    """A process that loads a table module alone leaves its foreign keys to other modules'
    tables unresolved, and the first flush touching that table fails with
    NoReferencedTableError (scripts/bootstrap_simulation_db.py, 2026-11-04 soak). The test
    fixtures import apps/schema.py, so only a fresh interpreter per entrypoint shows it."""
    unresolved, _ = _resolve_alone(module)
    assert unresolved == _KNOWN_UNRESOLVED.get(module, ""), \
        f"{module} leaves foreign keys unresolved:\n{unresolved}"


def test_the_scripts_package_registers_the_whole_schema() -> None:
    """Every script runs as `python -m scripts.<name>`, so the package import covers all of
    them, including those that import a table module inside a function."""
    unresolved, tables = _resolve_alone("scripts")
    _, complete = _resolve_alone("bfx_funding_bot.apps.schema")
    assert (unresolved, tables) == ("", complete)
