"""db388a84678f: the realm guard fills an omitted ``deployment_environment``; narrow twin keys.

On a disposable PostgreSQL at that revision: an INSERT that omits the column gets the stamp,
a row of another realm and a write to an unstamped database are still refused. From the
previous revision: a row of another realm, or any row in an unstamped database, refuses the
upgrade and leaves the schema where it was (an unguarded table does not block); otherwise
every key and index that includes the column gains a ``_narrow`` twin over the same columns
without it. At head: the same coverage by name, and the guard fires before every other row
trigger that reads the column.

Mutations: restore the guard body that does not fill (the fill test fails); drop the realm
assertion (both refusal tests fail); scan every table with the column (the unguarded-table
test fails); drop or misspell one ``NARROW_INDEXES`` entry (the twin tests fail).
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.pg_templates import DISABLE_REALM_TRIGGERS_SQL, alembic, stamp_realm

pytestmark = pytest.mark.integration

_REVISION = "db388a84678f"
_BEFORE = "96535191e443"
_COLUMN = "deployment_environment"
_NO_TWIN_NAME = "ix_ledger_observation_query_scope_revision"
_NO_TWIN = "(exchange_account_id, deployment_environment, query_revision DESC)"

_SIM_EVENT_WITHOUT_REALM = (
    "INSERT INTO sim_venue_event(exchange_account_id, seq, event_type, schema_version, payload) "
    "VALUES ('sim-1', 1, 'wallet_funded', 1, '{}')"
)
_SIM_EVENT_PROD = (
    "INSERT INTO sim_venue_event(exchange_account_id, deployment_environment, seq, event_type, "
    "schema_version, payload) VALUES ('sim-1', 'prod', 1, 'wallet_funded', 1, '{}')"
)


def _legacy_cell(realm: str) -> str:
    # A live realm table without foreign keys or a realm CHECK.
    return (
        "INSERT INTO attribution_legacy_offer_cells(exchange_account_id, deployment_environment, "
        f"venue_offer_id, cell) VALUES ('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', '{realm}', 'o1', 'c1')"
    )


@pytest.fixture
def before_url(pg_templates: Any, pg_clone: Any) -> str:
    """An empty, unstamped database at the previous revision."""
    return pg_clone(pg_templates.template(
        "narrow_keys_before", lambda url: alembic(url, "upgrade", _BEFORE)))


@pytest.fixture
def unstamped_url(pg_templates: Any, pg_clone: Any) -> str:
    """An empty, unstamped database at this revision."""
    return pg_clone(pg_templates.template(
        "narrow_keys_at", lambda url: alembic(url, "upgrade", _REVISION)))


def _run(url: str, *statements: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)
    finally:
        engine.dispose()


def _all(url: str, sql: str) -> list[Any]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql)).tuples().all())
    finally:
        engine.dispose()


def _narrow_indexes(url: str) -> list[str]:
    return [name for (name,) in _all(
        url, "SELECT indexname FROM pg_indexes WHERE indexname LIKE '%\\_narrow' ORDER BY 1")]


def test_an_insert_that_omits_the_realm_gets_the_stamp(unstamped_url: str) -> None:
    stamp_realm(unstamped_url, "ci")
    _run(unstamped_url, _SIM_EVENT_WITHOUT_REALM)
    assert _all(unstamped_url, f"SELECT {_COLUMN} FROM sim_venue_event") == [("ci",)]


def test_a_row_of_another_realm_is_still_refused(unstamped_url: str) -> None:
    stamp_realm(unstamped_url, "ci")
    with pytest.raises(DBAPIError, match="database realm ci refuses a write of realm prod"):
        _run(unstamped_url, _SIM_EVENT_PROD)


def test_an_unstamped_database_still_refuses_the_write(unstamped_url: str) -> None:
    with pytest.raises(DBAPIError, match="database realm is not stamped"):
        _run(unstamped_url, _SIM_EVENT_WITHOUT_REALM)


def _refuses(url: str, match: str) -> None:
    with pytest.raises(
        RuntimeError, match=match + r" public\.attribution_legacy_offer_cells$",
    ):
        alembic(url, "upgrade", _REVISION)
    assert _all(url, "SELECT version_num FROM alembic_version") == [(_BEFORE,)]
    assert _narrow_indexes(url) == []


def test_a_row_of_a_second_realm_refuses_the_upgrade(before_url: str) -> None:
    stamp_realm(before_url, "ci")
    # A ci database whose trigger was switched off, in this visible statement, to plant it.
    _run(before_url, _legacy_cell("ci").replace("'o1'", "'o0'"),
         DISABLE_REALM_TRIGGERS_SQL, _legacy_cell("prod"))
    _refuses(before_url, "rows of a realm other than the database_realm stamp in")


def test_a_row_in_an_unstamped_database_refuses_the_upgrade(before_url: str) -> None:
    _run(before_url, DISABLE_REALM_TRIGGERS_SQL, _legacy_cell("ci"))
    _refuses(before_url, r"not stamped \(database_realm is empty\) but holds rows in")


def test_a_row_of_another_realm_in_an_unguarded_table_does_not_block(before_url: str) -> None:
    # A scratch copy without the realm trigger is not live realm data (as in 96535191e443).
    stamp_realm(before_url, "ci")
    _run(
        before_url,
        "CREATE TABLE public.scratch_copy (deployment_environment text NOT NULL)",
        "INSERT INTO public.scratch_copy VALUES ('prod')",
    )
    alembic(before_url, "upgrade", _REVISION)
    assert _all(before_url, "SELECT version_num FROM alembic_version") == [(_REVISION,)]


def test_every_key_with_the_realm_gains_a_twin_without_it(before_url: str) -> None:
    stamp_realm(before_url, "ci")
    _run(before_url, _legacy_cell("ci"))
    alembic(before_url, "upgrade", _REVISION)

    # Every index of a live table that includes the column (constraint-backed or not).
    wide = dict(_all(before_url, f"""
        SELECT ic.relname, pg_get_indexdef(i.indexrelid)
        FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = '{_COLUMN}'
        WHERE a.attnum = ANY (i.indkey::int2[])
          AND n.nspname NOT IN ('legacy_archive', 'release_archive')"""))
    narrow = dict(_all(before_url, """
        SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE '%\\_narrow'"""))

    def without_realm(name: str, definition: str) -> str:
        columns = definition.replace(f"{_COLUMN}, ", "").replace(f", {_COLUMN}", "")
        return columns.replace(f"INDEX {name} ON", f"INDEX {name}_narrow ON")

    # The unique twin read backward serves it; step 3 drops it.
    assert _NO_TWIN in wide.pop(_NO_TWIN_NAME)
    assert len(wide) == 37
    assert narrow == {f"{name}_narrow": without_realm(name, d) for name, d in wide.items()}
    assert all(_COLUMN not in definition for definition in narrow.values())


def test_at_head_every_key_with_the_realm_has_its_twin(pg_head_url: str) -> None:
    """A key or index with the column added by a later revision needs a twin too."""
    missing = _all(pg_head_url, f"""
        SELECT ic.relname
        FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = '{_COLUMN}'
        WHERE a.attnum = ANY (i.indkey::int2[])
          AND n.nspname NOT IN ('legacy_archive', 'release_archive')
          AND ic.relname <> '{_NO_TWIN_NAME}'
          AND to_regclass(format('%I.%I', n.nspname, ic.relname || '_narrow')) IS NULL""")
    assert missing == []


def test_the_guard_fires_before_every_other_trigger_that_reads_the_realm(pg_head_url: str) -> None:
    """BEFORE ROW triggers fire in name order; one that reads the column ahead of
    ``database_realm_write`` would see the NULL of an INSERT that omits it."""
    early = _all(pg_head_url, f"""
        SELECT t.tgrelid::regclass::text, t.tgname
        FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE NOT t.tgisinternal AND p.prosrc ~ '{_COLUMN}'
          AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2 AND (t.tgtype & 4) = 4
          AND t.tgname < 'database_realm_write'
          AND EXISTS (SELECT 1 FROM pg_trigger g WHERE g.tgrelid = t.tgrelid
                      AND g.tgname = 'database_realm_write')""")
    assert early == []
