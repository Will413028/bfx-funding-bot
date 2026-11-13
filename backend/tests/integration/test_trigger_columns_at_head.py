"""At head, every ``NEW.<col>`` / ``OLD.<col>`` a plpgsql trigger reads exists on its table.

A migration that drops or renames a column passes even when a trigger still reads it:
plpgsql resolves the field only when the trigger fires. This reads every user trigger's
function body (``pg_proc.prosrc``) from a database migrated to head and checks it against
the attached tables' live columns. The parsing (``TG_TABLE_NAME`` branches, comments,
literals) is unit-tested on fixture bodies in ``tests/test_trigger_columns.py``, which
holds the present / dropped column pair that shows this check can fail.
"""
from __future__ import annotations

from collections import defaultdict

import pytest
from sqlalchemy import create_engine, text

from tests.trigger_columns import missing_columns

pytestmark = pytest.mark.integration

_TRIGGERS = text("""
    SELECT p.oid::regprocedure::text AS function, p.prosrc, t.tgrelid::regclass::text AS tbl,
           c.relname
      FROM pg_trigger t
      JOIN pg_proc p ON p.oid = t.tgfoid
      JOIN pg_language l ON l.oid = p.prolang AND l.lanname = 'plpgsql'
      JOIN pg_class c ON c.oid = t.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE NOT t.tgisinternal
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
""")
_COLUMNS = text("""
    SELECT a.attrelid::regclass::text AS tbl, a.attname
      FROM pg_attribute a
     WHERE a.attrelid IN (SELECT tgrelid FROM pg_trigger WHERE NOT tgisinternal)
       AND a.attnum > 0 AND NOT a.attisdropped
""")


def test_every_trigger_reads_only_columns_its_table_has(pg_head_url: str) -> None:
    engine = create_engine(pg_head_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(_TRIGGERS).all()
            columns: dict[str, set[str]] = defaultdict(set)
            for tbl, attname in conn.execute(_COLUMNS):
                columns[tbl].add(attname)
    finally:
        engine.dispose()

    bodies: dict[str, str] = {}
    # Key tables by the schema-qualified name (two schemas may share a relname);
    # TG_TABLE_NAME compares against the bare relname, passed alongside.
    attached: dict[str, dict[str, set[str]]] = defaultdict(dict)
    relnames: dict[str, str] = {}
    for function, prosrc, tbl, relname in rows:
        bodies[function] = prosrc
        attached[function][tbl] = columns[tbl]
        relnames[tbl] = relname

    assert rows, "no plpgsql triggers found at head: the catalog query is wrong"
    broken = {
        f"{function} on {t}": sorted(cols)
        for function, tables in attached.items()
        for t, cols in missing_columns(bodies[function], tables, relnames).items()
    }
    assert not broken, f"trigger functions read columns their tables do not have: {broken}"
