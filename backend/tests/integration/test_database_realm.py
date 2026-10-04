"""``database_realm``: one stamp per database, and a trigger that rejects foreign-realm writes.

Mutations (one at a time, revert after each, run this file):

* Drop ``database_realm_write`` from one table in ``REALM_TABLES``: the scan test fails.
* The function allows an unstamped database (``stamp IS NULL`` no longer raises): the
  unstamped tests fail.
* The function exempts the owner (``current_user`` is the table owner): the owner tests fail.
* The migration stamps a mixed database (take the first realm instead of refusing): the
  mixed-database test fails.
* The migration derives from all tables but one: the derivation test of that table fails.
* Allow UPDATE on the stamp (drop ``immutable_database_realm_write``): the immutability test fails.
* Grant ``bfx_webapi`` (or ``bfx_bot``) INSERT on ``database_realm``: the grants test fails.
* Attach the trigger as ``BEFORE INSERT`` only: the UPDATE-of-the-column test fails.
* Drop ``SET search_path`` from the function: the hardening test fails.
* Skip one schema in the scan query (``projection_audit``): the scan test fails.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, InternalError, ProgrammingError

from bfx_funding_bot.core.database_realm import DATABASE_REALM_TABLE, KNOWN_REALMS
from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_REVISION = "a7c3e9f1b2d4"
_PREVIOUS = "e6b1d4a7c9f3"
_TRIGGER = "database_realm_write"
_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic/versions"


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("realm_migration", _VERSIONS / f"{_REVISION}_database_realm.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reset_with_roles(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        for role in ("bfx_webauth", "bfx_cutover_reader"):
            conn.exec_driver_sql(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') "
                f"THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
    engine.dispose()


def _build_unstamped(url: str) -> None:
    _reset_with_roles(url)
    alembic(url, "upgrade", "head")
    alembic(url, "check")


def _build_previous(url: str) -> None:
    _reset_with_roles(url)
    alembic(url, "upgrade", _PREVIOUS)


def _build_ci(url: str) -> None:
    stamp_realm(url, "ci")


@pytest.fixture
def unstamped(pg_templates: Any, pg_clone: Any) -> Iterator[Any]:
    engine = create_engine(pg_clone(pg_templates.template("realm_unstamped", _build_unstamped)))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def ci_db(pg_templates: Any, pg_clone: Any) -> Iterator[Any]:
    pg_templates.template("realm_unstamped", _build_unstamped)
    name = pg_templates.template("realm_ci", _build_ci, base="realm_unstamped")
    engine = create_engine(pg_clone(name))
    with engine.begin() as conn:
        conn.exec_driver_sql(f"INSERT INTO exchange_accounts(id, venue, label) VALUES ('{_ACCOUNT}', 'bitfinex', 'realm')")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def previous_url(pg_templates: Any, pg_clone: Any) -> str:
    return pg_clone(pg_templates.template("realm_previous", _build_previous))


def _nav_peak(realm: str, symbol: str = "fUST") -> str:
    return (
        "INSERT INTO nav_peak(account_id, exchange_account_id, deployment_environment, symbol, peak, "
        f"updated_at_ms) VALUES ('{_ACCOUNT}', '{_ACCOUNT}', '{realm}', '{symbol}', 1, 1)"
    )


def _event_log(realm: str) -> str:
    return (
        "INSERT INTO event_log(account_id, exchange_account_id, deployment_environment, event_type, "
        f"payload, occurred_at_ms) VALUES ('{_ACCOUNT}', '{_ACCOUNT}', '{realm}', 'x', '{{}}', 1)"
    )


def _request(realm: str) -> str:
    return (
        "INSERT INTO trading_control_requests(request_id, exchange_account_id, deployment_environment, "
        f"action, reason, requested_by, created_at_ms) VALUES ('{uuid4()}', '{_ACCOUNT}', '{realm}', "
        "'resume', 'r', 'operator', 1)"
    )


def _sim_event(realm: str) -> str:
    return (
        "INSERT INTO sim_venue_event(exchange_account_id, deployment_environment, seq, event_type, "
        f"schema_version, payload) VALUES ('sim-1', '{realm}', 1, 'wallet_funded', 1, '{{}}')"
    )


def _run(engine: Any, sql: str, *, role: str | None = None) -> None:
    with engine.begin() as conn:
        if role:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        conn.exec_driver_sql(sql)


def _refused(engine: Any, sql: str, match: str, *, role: str | None = None) -> None:
    with pytest.raises((InternalError, ProgrammingError, DBAPIError), match=match):
        _run(engine, sql, role=role)


# -- coverage ------------------------------------------------------------------------

def test_every_realm_column_has_the_trigger(ci_db: Any) -> None:
    migration = _migration()
    with ci_db.connect() as conn:
        columns = {
            f"{schema}.{table}" for schema, table in conn.execute(text(
                "SELECT c.table_schema, c.table_name FROM information_schema.columns c "
                "JOIN information_schema.tables t USING (table_schema, table_name) "
                "WHERE c.column_name = 'deployment_environment' AND t.table_type = 'BASE TABLE' "
                # release_archive holds frozen copies of retired tables, not live realm data
                # (the same exclusion as core/alembic_compare.py).
                "AND c.table_schema NOT IN ('pg_catalog', 'information_schema', 'release_archive')"))
        }
        triggered = {
            f"{schema}.{table}": (definition, enabled) for schema, table, definition, enabled in conn.execute(text(
                "SELECT n.nspname, c.relname, pg_get_triggerdef(t.oid), t.tgenabled "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE t.tgname = :name AND NOT t.tgisinternal"),
                {"name": _TRIGGER})
        }
    assert columns, "the scan found no realm table"
    assert columns == set(triggered), sorted(columns ^ set(triggered))
    assert columns == set(migration.REALM_TABLES)
    for table, (definition, enabled) in triggered.items():
        assert enabled == "O", table
        assert "BEFORE INSERT OR UPDATE OF deployment_environment ON" in definition, table
        assert "FOR EACH ROW" in definition, table
        assert "guard_database_realm()" in definition, table


def test_the_orm_realm_tables_are_the_migrations_list() -> None:
    from bfx_funding_bot.core.db import Base
    from bfx_funding_bot.modules.execution.projection_cutover.tables import ArchiveBase

    migration = _migration()
    orm = {
        f"{table.schema or 'public'}.{table.name}"
        for metadata in (Base.metadata, ArchiveBase.metadata) for table in metadata.tables.values()
        if "deployment_environment" in table.c
    }
    assert orm == set(migration.REALM_TABLES), sorted(orm ^ set(migration.REALM_TABLES))


# -- the stamp's derivation ----------------------------------------------------------

def _seed_previous(url: str, *sql: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"INSERT INTO exchange_accounts(id, venue, label) VALUES ('{_ACCOUNT}', 'bitfinex', 'realm')")
        for statement in sql:
            conn.exec_driver_sql(statement)
    engine.dispose()


def _stamp_of(url: str) -> list[tuple[str, str]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text("SELECT realm, actor FROM database_realm"))]
    finally:
        engine.dispose()


@pytest.mark.parametrize("sql", [_nav_peak("prod"), _event_log("prod")])
def test_the_migration_stamps_prod_from_prod_data(previous_url: str, sql: str) -> None:
    _seed_previous(previous_url, sql)
    alembic(previous_url, "upgrade", "head")
    assert _stamp_of(previous_url) == [("prod", f"migration {_REVISION}")]


@pytest.mark.parametrize(("sql", "realm"), [
    (_sim_event("ci"), "ci"), (_sim_event("shadow"), "shadow"), (_nav_peak("ci"), "ci"),
])
def test_the_migration_stamps_the_one_realm_it_finds(previous_url: str, sql: str, realm: str) -> None:
    _seed_previous(previous_url, sql)
    alembic(previous_url, "upgrade", "head")
    assert _stamp_of(previous_url) == [(realm, f"migration {_REVISION}")]


def test_the_migration_refuses_a_mixed_database_and_changes_nothing(previous_url: str) -> None:
    _seed_previous(previous_url, _nav_peak("prod"), _sim_event("shadow"))
    with pytest.raises(RuntimeError, match="more than one realm"):
        alembic(previous_url, "upgrade", "head")
    engine = create_engine(previous_url)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT to_regclass('public.{DATABASE_REALM_TABLE}')")) is None
            assert conn.scalar(text("SELECT to_regproc('public.guard_database_realm()')")) is None
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _PREVIOUS
    finally:
        engine.dispose()


def test_the_migration_refuses_a_value_that_is_no_realm(previous_url: str) -> None:
    _seed_previous(previous_url, _nav_peak("legacy"))
    with pytest.raises(RuntimeError, match="hold 'legacy'"):
        alembic(previous_url, "upgrade", "head")


def test_an_empty_database_stays_unstamped(unstamped: Any) -> None:
    with unstamped.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM database_realm")) == 0


# -- fail-closed and the realm comparison ---------------------------------------------

@pytest.mark.parametrize("sql", [_nav_peak("ci"), _nav_peak("prod"), _sim_event("ci")])
def test_an_unstamped_database_refuses_every_realm_write_even_from_the_owner(unstamped: Any, sql: str) -> None:
    _refused(unstamped, sql, "database realm is not stamped")


def test_an_unstamped_database_refuses_the_bot_and_the_web_api(unstamped: Any) -> None:
    _refused(unstamped, _event_log("ci"), "database realm is not stamped", role="bfx_bot")
    with unstamped.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM event_log")) == 0


@pytest.mark.parametrize("foreign", ["prod", "shadow"])
def test_the_owner_is_not_exempt_on_insert(ci_db: Any, foreign: str) -> None:
    _run(ci_db, _nav_peak("ci"))
    _refused(ci_db, _nav_peak(foreign, "fUSD"), f"database realm ci refuses a write of realm {foreign} to nav_peak")


def test_the_owner_is_not_exempt_on_the_simulated_venue_table(ci_db: Any) -> None:
    _run(ci_db, _sim_event("ci"))
    _refused(ci_db, _sim_event("shadow").replace("'sim-1'", "'sim-2'"), "database realm ci refuses a write of realm shadow")


def test_updating_the_realm_column_is_checked_but_other_columns_are_not(ci_db: Any) -> None:
    _run(ci_db, _nav_peak("ci"))
    _run(ci_db, "UPDATE nav_peak SET peak = 2")
    _run(ci_db, "UPDATE nav_peak SET deployment_environment = 'ci'")
    for foreign in ("prod", "shadow"):
        _refused(ci_db, f"UPDATE nav_peak SET deployment_environment = '{foreign}'", "refuses a write of realm")
    with ci_db.connect() as conn:
        assert conn.scalar(text("SELECT deployment_environment FROM nav_peak")) == "ci"


def test_the_bot_role_is_checked_on_insert(ci_db: Any) -> None:
    _run(ci_db, _event_log("ci"), role="bfx_bot")
    _refused(ci_db, _event_log("prod"), "database realm ci refuses a write of realm prod to event_log", role="bfx_bot")


def test_the_web_api_is_checked_without_any_privilege_on_the_stamp(ci_db: Any) -> None:
    with ci_db.connect() as conn:
        assert not conn.scalar(text("SELECT has_table_privilege('bfx_webapi', 'public.database_realm', 'SELECT')"))
    _run(ci_db, _request("ci"), role="bfx_webapi")
    _refused(ci_db, _request("shadow"), "database realm ci refuses a write of realm shadow", role="bfx_webapi")


def test_a_deliberate_exception_is_the_visible_disable_trigger(ci_db: Any) -> None:
    with ci_db.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE nav_peak DISABLE TRIGGER {_TRIGGER}")
        conn.exec_driver_sql(_nav_peak("prod"))
        conn.exec_driver_sql(f"ALTER TABLE nav_peak ENABLE TRIGGER {_TRIGGER}")
    _refused(ci_db, _nav_peak("prod", "fUSD"), "refuses a write of realm prod")


# -- the stamp itself ------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE database_realm SET realm = 'prod'",
    "DELETE FROM database_realm",
    "TRUNCATE database_realm",
])
def test_the_stamp_is_immutable_even_for_the_owner(ci_db: Any, statement: str) -> None:
    _refused(ci_db, statement, "immutable database realm stamp")
    with ci_db.connect() as conn:
        assert conn.scalar(text("SELECT realm FROM database_realm")) == "ci"


def test_there_is_one_stamp_and_only_a_known_realm(ci_db: Any) -> None:
    insert = "INSERT INTO database_realm(realm, stamped_at_ms, actor) VALUES ('{}', 1, '{}')"
    with pytest.raises(IntegrityError, match="database_realm_pkey"):
        _run(ci_db, insert.format("shadow", "again"))
    with pytest.raises(IntegrityError, match="ck_database_realm_singleton"):
        _run(ci_db, "INSERT INTO database_realm(id, realm, stamped_at_ms, actor) VALUES (false, 'ci', 1, 'x')")


@pytest.mark.parametrize("realm", ["live", "PROD", "", "paper"])
def test_the_stamp_names_only_a_known_realm(unstamped: Any, realm: str) -> None:
    with pytest.raises(IntegrityError, match="ck_database_realm_realm"):
        _run(unstamped, f"INSERT INTO database_realm(realm, stamped_at_ms, actor) VALUES ('{realm}', 1, 'x')")
    with pytest.raises(IntegrityError, match="ck_database_realm_actor"):
        _run(unstamped, "INSERT INTO database_realm(realm, stamped_at_ms, actor) VALUES ('ci', 1, '')")
    assert set(KNOWN_REALMS) == {"prod", "shadow", "ci"}


def test_runtime_roles_cannot_write_the_stamp_and_only_the_bot_reads_it(ci_db: Any) -> None:
    with ci_db.connect() as conn:
        for role in _ROLES:
            for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                assert not conn.scalar(
                    text("SELECT has_any_column_privilege(:r, 'public.database_realm', :p)"),
                    {"r": role, "p": privilege})
            for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
                assert not conn.scalar(
                    text("SELECT has_table_privilege(:r, 'public.database_realm', :p)"), {"r": role, "p": privilege})
        assert conn.scalar(text("SELECT has_table_privilege('bfx_bot', 'public.database_realm', 'SELECT')"))
        for role in ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
            assert not conn.scalar(
                text("SELECT has_any_column_privilege(:r, 'public.database_realm', 'SELECT')"), {"r": role})
        owner = conn.scalar(text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'public.database_realm'::regclass"))
        granted = {
            (grantee, privilege) for grantee, privilege in conn.execute(text(
                "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, a.privilege_type "
                "FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = 'public.database_realm'::regclass"))
            if grantee != owner
        }
        assert granted == {("bfx_bot", "SELECT")}
    for role in ("bfx_bot", "bfx_webapi"):
        _refused(ci_db, "INSERT INTO database_realm(realm, stamped_at_ms, actor) VALUES ('ci', 1, 'x')",
                 "permission denied", role=role)
        _refused(ci_db, "UPDATE database_realm SET realm = 'prod'", "permission denied", role=role)
    with ci_db.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        assert conn.scalar(text("SELECT realm FROM database_realm")) == "ci"


def test_the_trigger_functions_are_hardened(ci_db: Any) -> None:
    with ci_db.connect() as conn:
        for function in ("guard_database_realm", "reject_database_realm_mutation"):
            config, acl, definer = conn.execute(text(
                "SELECT proconfig, proacl::text[], prosecdef FROM pg_proc WHERE proname = :f"), {"f": function}).one()
            assert config == ["search_path=pg_catalog"], function
            assert not any(entry.startswith("=") for entry in (acl or ())), (function, acl)
            assert definer is (function == "guard_database_realm")
            for role in _ROLES:
                assert not conn.scalar(
                    text(f"SELECT has_function_privilege(:r, 'public.{function}()', 'EXECUTE')"), {"r": role})


# -- schema bookkeeping -----------------------------------------------------------------

def test_alembic_check_is_clean_and_the_downgrade_round_trips(ci_db: Any) -> None:
    url = ci_db.url.render_as_string(hide_password=False)
    _run(ci_db, _nav_peak("ci"))
    alembic(url, "check")
    alembic(url, "downgrade", _PREVIOUS)
    with ci_db.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.database_realm')")) is None
        assert conn.scalar(text("SELECT to_regproc('public.guard_database_realm()')")) is None
        assert conn.scalar(text("SELECT to_regproc('public.reject_database_realm_mutation()')")) is None
        assert conn.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname = :n"), {"n": _TRIGGER}) == 0
        assert conn.scalar(text("SELECT count(*) FROM nav_peak")) == 1  # row data untouched
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    assert _stamp_of(url) == [("ci", f"migration {_REVISION}")]  # derived again from the data
    with ci_db.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname = :n"), {"n": _TRIGGER}) == len(
            _migration().REALM_TABLES)
