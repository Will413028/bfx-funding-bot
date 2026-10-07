"""``apps/schema.py``: importing it registers a complete schema.

Mutations (one at a time; revert after each): drop ``ledger.tables`` from apps/schema.py (both
tests: other modules' foreign keys point at its tables); drop ``simulated_venue.tables``
(``test_every_table_module_is_in_the_schema``).
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from bfx_funding_bot.core.db import Base

_SRC = Path(__file__).resolve().parents[2] / "src"

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
