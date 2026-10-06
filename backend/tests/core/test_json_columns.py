"""Every JSON column is ``JSON_DOCUMENT``: Python ``None`` is SQL NULL, and the database refuses
the JSON literal ``null``.

Without ``none_as_null`` SQLAlchemy writes ``None`` as ``'null'::jsonb``, which ``IS NULL`` does
not see (a live attempt's ``seed_provenance`` read as seeded). The table modules are the ones
``alembic/env.py`` registers, so a new table cannot slip past.
"""
import importlib
import importlib.util
import re
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.types import JSON

from bfx_funding_bot.core.db import JSON_DOCUMENT, Base

_BACKEND = Path(__file__).resolve().parents[2]
_MIGRATION = _BACKEND / "alembic/versions/a6c7e8f9b0d1_ledger_json_sql_null.py"

for _module in re.findall(r"^import (bfx_funding_bot\.\S+)$",
                          (_BACKEND / "alembic/env.py").read_text(), re.M):
    importlib.import_module(_module)


def _json_columns():
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type.dialect_impl(postgresql.dialect()), JSON):
                yield table, column


def _checked(table, column) -> bool:
    # The frozen archive is written by no role; a generated column by the server alone.
    return table.schema is None and column.computed is None


def test_every_json_column_is_the_document_type() -> None:
    columns = list(_json_columns())
    assert columns
    for table, column in columns:
        assert column.type is JSON_DOCUMENT, (table.fullname, column.name)
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            assert column.type.dialect_impl(dialect).none_as_null, (table.fullname, column.name)


def test_every_written_json_column_refuses_the_json_literal() -> None:
    for table, column in _json_columns():
        name = f"ck_{table.name}_{column.name}_json"
        checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)}
        if not _checked(table, column):
            assert name not in checks
            continue
        assert checks.get(name) == f"jsonb_typeof({column.name}) <> 'null'", (table.fullname, name)
        assert len(name) <= 63, name


def test_the_migration_covers_exactly_those_columns() -> None:
    spec = importlib.util.spec_from_file_location("json_null_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    expected = {(t.name, c.name, bool(c.nullable)) for t, c in _json_columns() if _checked(t, c)}
    assert set(migration.COLUMNS) == expected
