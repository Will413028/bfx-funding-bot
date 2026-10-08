"""`sim_venue_event` on clone PostgreSQL: realm CHECK, insert-only, exact grants.

Two builds of the database: ``worst_case`` hands every runtime role ALL on new tables
through default privileges; ``prod_faithful`` is the production host setup
(``docs/runbooks/fresh-host-setup.md``: ``bfx_bot`` DML by default, nothing for the web API).

Mutations (one at a time, revert after each, run this file):

* Drop ``immutable_sim_venue_write`` in the migration: the UPDATE and DELETE tests fail.
* Drop ``immutable_sim_venue_truncate``: the TRUNCATE test fails.
* Skip the ``REVOKE ALL ... FROM {role}`` loop for ``bfx_bot``: the exact-grant test fails
  (UPDATE and DELETE stay granted by default privileges) and so does the denied-DML test.
* Drop ``ck_sim_venue_event_realm`` (migration and model): the ``prod`` insert succeeds.
* Drop ``SET search_path`` from the function: the hardening test fails.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

from tests.pg_templates import (
    LAST_REVERSIBLE_REVISION,
    alembic,
    disable_realm_triggers,
    stamp_realm,
    template_at,
)

from .test_ledger_schema_roles import pre_switch
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_PREVIOUS = "a3b4c5d6e7f8"
_TABLE = "sim_venue_event"
_FUNCTION = "reject_sim_venue_mutation"
_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")  # the cutover reader is retired at head
_ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_ACCOUNT = "sim-account-1"


def _row(realm: str = "ci", seq: int = 1, account: str = _ACCOUNT) -> str:
    return (
        "INSERT INTO sim_venue_event (exchange_account_id, deployment_environment, seq, "
        "event_type, schema_version, payload) VALUES "
        f"('{account}', '{realm}', {seq}, 'wallet_funded', 1, '{{}}')"
    )


def _roles(conn: Any) -> None:
    conn.exec_driver_sql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
        "THEN CREATE ROLE bfx_webauth; END IF; END $$")
    conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_webauth")


def _prepare_worst_case(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _roles(conn)
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_webauth")
    engine.dispose()


def _prepare_prod_faithful(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _roles(conn)
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_bot")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_webapi")
    engine.dispose()


def _at_head(prepare: Callable[[str], None]) -> Callable[[str], None]:
    def build(url: str) -> None:
        prepare(url)
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        stamp_realm(url, "ci")

    return build


_PREPARES = {"sim_venue_worst_case": _prepare_worst_case,
             "sim_venue_prod_faithful": _prepare_prod_faithful}
_BUILDS = {name: _at_head(prepare) for name, prepare in _PREPARES.items()}
# The test that downgrades starts from the last reversible revision, not head.
_REVERSIBLE_BUILDS = {f"{name}_reversible": template_at(LAST_REVERSIBLE_REVISION, prepare)
                      for name, prepare in _PREPARES.items()}


@pytest.fixture(params=sorted(_BUILDS))
def db(request: Any, pg_templates: Any, pg_clone: Any) -> Iterator[tuple[str, Any]]:
    yield from _db(pg_clone(pg_templates.template(request.param, _BUILDS[request.param])))


@pytest.fixture(params=sorted(_REVERSIBLE_BUILDS))
def reversible_db(request: Any, pg_templates: Any, pg_clone: Any) -> Iterator[tuple[str, Any]]:
    yield from _db(pg_clone(pg_templates.template(request.param,
                                                  _REVERSIBLE_BUILDS[request.param])))


def _db(url: str) -> Iterator[tuple[str, Any]]:
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()


def _as(role: str, engine: Any, sql: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        conn.exec_driver_sql(sql)


def test_alembic_check_reports_no_drift(db: Any) -> None:
    # Covered again by both template builds; this keeps the claim visible by name.
    url, _ = db
    alembic(url, "check")


@pytest.mark.parametrize("realm", ["prod", "PROD", "", "paper", "live"])
def test_realm_check_rejects_every_realm_but_shadow_and_ci(db: Any, realm: str) -> None:
    _, engine = db
    disable_realm_triggers(engine)  # the database stamp refuses these first; this tests the CHECK itself
    with pytest.raises(IntegrityError, match="ck_sim_venue_event_realm"), engine.begin() as conn:
        conn.exec_driver_sql(_row(realm))
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM sim_venue_event")) == 0


def test_shadow_and_ci_rows_are_accepted_and_scopes_are_independent(db: Any) -> None:
    _, engine = db
    disable_realm_triggers(engine)  # one database holds one realm; the CHECK itself admits both
    with engine.begin() as conn:
        for realm in ("shadow", "ci"):
            conn.exec_driver_sql(_row(realm, 1))
            conn.exec_driver_sql(_row(realm, 2))
        conn.exec_driver_sql(_row("ci", 1, account="sim-account-2"))
    with pytest.raises(IntegrityError, match="pk_sim_venue_event"), engine.begin() as conn:
        conn.exec_driver_sql(_row("ci", 2))  # the same scope and position twice


@pytest.mark.parametrize(("sql", "constraint"), [
    (_row(seq=0), "ck_sim_venue_event_seq"),
    (_row(account=""), "ck_sim_venue_event_account"),
    (_row().replace("'wallet_funded', 1,", "'wallet_funded', 0,"), "ck_sim_venue_event_version"),
])
def test_the_other_checks_reject_nonsense(db: Any, sql: str, constraint: str) -> None:
    _, engine = db
    with pytest.raises(IntegrityError, match=constraint), engine.begin() as conn:
        conn.exec_driver_sql(sql)


@pytest.mark.parametrize("statement", [
    "UPDATE sim_venue_event SET event_type = 'x'",
    "DELETE FROM sim_venue_event",
    "TRUNCATE sim_venue_event",
])
def test_the_owner_cannot_update_delete_or_truncate(db: Any, statement: str) -> None:
    _, engine = db
    with engine.begin() as conn:
        conn.exec_driver_sql(_row())
    with (
        pytest.raises((InternalError, ProgrammingError), match="immutable simulated venue event log"),
        engine.begin() as conn,
    ):
        conn.exec_driver_sql(statement)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT event_type FROM sim_venue_event")) == "wallet_funded"


def _held(conn: Any, role: str) -> set[str]:
    return {
        p for p in _ALL_PRIVILEGES
        if conn.scalar(text("SELECT has_table_privilege(:r, 'public.sim_venue_event', :p)"),
                       {"r": role, "p": p})
    }


def test_bfx_bot_holds_exactly_select_and_insert(db: Any) -> None:
    _, engine = db
    with engine.connect() as conn:
        assert _held(conn, "bfx_bot") == {"SELECT", "INSERT"}
        for privilege in ("UPDATE", "REFERENCES"):
            assert not conn.scalar(
                text("SELECT has_any_column_privilege('bfx_bot','public.sim_venue_event',:p)"),
                {"p": privilege})


def test_bfx_bot_dml_beyond_select_and_insert_is_denied_in_a_read_write_transaction(db: Any) -> None:
    _, engine = db
    _as("bfx_bot", engine, _row())  # INSERT is the one write it has
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        assert conn.scalar(text("SELECT count(*) FROM sim_venue_event")) == 1
    for statement in (
        "UPDATE sim_venue_event SET event_type = 'x'",
        "DELETE FROM sim_venue_event",
        "TRUNCATE sim_venue_event",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            _as("bfx_bot", engine, statement)


def test_bfx_bot_writes_while_the_authority_epoch_is_legacy(db: Any) -> None:
    # The table is deliberately not behind the ledger dormancy triggers.
    _, engine = db
    with engine.begin() as conn:
        pre_switch(conn)  # a database the switch has not happened on
    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT authority FROM capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1")
        ) == "legacy"
    _as("bfx_bot", engine, _row("ci"))


@pytest.mark.parametrize("role", ["bfx_webapi", "bfx_webauth"])
def test_other_runtime_roles_hold_nothing(db: Any, role: str) -> None:
    _, engine = db
    with engine.connect() as conn:
        assert _held(conn, role) == set()
        for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            assert not conn.scalar(
                text("SELECT has_any_column_privilege(:r,'public.sim_venue_event',:p)"),
                {"r": role, "p": privilege})
    for statement in ("SELECT count(*) FROM sim_venue_event", _row()):
        with pytest.raises(ProgrammingError, match="permission denied"):
            _as(role, engine, statement)


def test_the_acl_names_only_the_owner_and_bfx_bot(db: Any) -> None:
    _, engine = db
    with engine.connect() as conn:
        owner = conn.scalar(text("SELECT pg_get_userbyid(relowner) FROM pg_class "
                                 "WHERE oid = 'public.sim_venue_event'::regclass"))
        granted = {
            (grantee, privilege) for grantee, privilege in conn.execute(text(
                "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, "
                "a.privilege_type FROM pg_class c, aclexplode(c.relacl) a "
                "WHERE c.oid = 'public.sim_venue_event'::regclass"))
            if grantee != owner
        }
        assert granted == {("bfx_bot", "SELECT"), ("bfx_bot", "INSERT")}
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.sim_venue_event'::regclass "
            "AND attacl IS NOT NULL")) == 0
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_class WHERE relkind = 'S' AND relname LIKE 'sim_venue_event%'")) == 0


def test_the_trigger_function_is_hardened(db: Any) -> None:
    _, engine = db
    with engine.connect() as conn:
        config, acl = conn.execute(text(
            f"SELECT proconfig, proacl::text[] FROM pg_proc WHERE proname = '{_FUNCTION}'")).one()
        assert config == ["search_path=pg_catalog"]
        assert not any(entry.startswith("=") for entry in (acl or ())), acl  # no PUBLIC execute
        for role in _ROLES:
            assert not conn.scalar(text(f"SELECT has_function_privilege(:r, 'public.{_FUNCTION}()', 'EXECUTE')"),
                                   {"r": role})


def test_downgrade_round_trip_and_populated_refusal(reversible_db: Any) -> None:
    url, engine = reversible_db
    alembic(url, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.sim_venue_event')")) is None
        assert conn.scalar(text(f"SELECT to_regproc('public.{_FUNCTION}')")) is None
    # Back to where the downgrade started, so the second one starts there too; the drift check
    # runs at head at the end.
    alembic(url, "upgrade", LAST_REVERSIBLE_REVISION)
    stamp_realm(url, "ci")
    with engine.begin() as conn:
        conn.exec_driver_sql(_row())
    with pytest.raises(RuntimeError, match="refusing to drop the log"):
        alembic(url, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM sim_venue_event")) == 1
    alembic(url, "upgrade", "head")
    alembic(url, "check")
