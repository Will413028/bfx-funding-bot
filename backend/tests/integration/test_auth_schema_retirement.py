"""``f836fdee9bf3`` (9d-7): ``auth``, ``bfx_webauth`` and the Better Auth memberships go.

Each test seeds an old-shape database at ``89886d27750b`` (the head before it), with
``bfx_webauth`` granted as fresh-host-setup §1c gave it: USAGE on ``auth``, the table and
sequence privileges, and the owner's two default privileges in ``auth``. The account has the
tailnet (numeric) owner membership W3 wrote and the Better Auth operator membership it
replaces; ``auth."user"`` holds that user, and ``user_profiles``, ``user_configs`` and
``api_keys`` one row each, keyed by the old ID (production has none; non-zero rows are what
shows the counts survive).

* upgrade: ``auth`` is gone with its default privileges, the non-numeric membership is
  deleted and the numeric one kept (``operator_authorized`` still holds), the legacy rows are
  all there, and ``bfx_webauth`` holds nothing here; on a cluster of its own (as in
  production) the role is dropped;
* refusals, each leaving the database at ``89886d27750b`` with ``auth`` and the memberships
  untouched: (A) a live account whose only operator is non-numeric; (B) a ``requested`` row
  from a non-numeric ``requested_by`` in each outbox; (C) an object outside ``auth`` that
  CASCADE would take with it: a view, a function body, a column default, a trigger; (D) a
  second foreign key into ``auth``; (F) a row lost from a legacy table during the drop.

Mutation checks (one at a time; revert after each):

* drop assertion A: ``test_a_live_account_without_a_numeric_operator_refuses`` fails;
* drop assertion B: every ``test_a_pending_request_from_an_old_identity_refuses`` case fails;
* drop the ``pg_depend`` check: ``test_an_object_outside_auth_that_depends_on_it_refuses``
  fails for the view, the column default and the trigger (the function case is still caught
  by the ``prosrc`` check);
* drop the foreign-key check: ``test_a_second_foreign_key_into_auth_refuses`` fails (the
  ``pg_depend`` check still refuses the extra FK, with its own message, so the schema stays);
* drop the row-count check: ``test_a_row_lost_during_the_drop_refuses`` fails.

The deleted-equals-counted check on the memberships has no failing mutation: under the
SHARE ROW EXCLUSIVE lock nothing can change the rows between the count and the DELETE, so it
only guards a later edit of the migration.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from pytest_postgresql.factories import postgresql_proc
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import Pool

from tests import pg_local
from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_BEFORE = "89886d27750b"
_RETIRE = "f836fdee9bf3"
WEBAUTH = "bfx_webauth"
_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_NODE = "1234567890"
_OLD = "ba-user-old-id"
_OUTBOXES = ("trading_control_requests", "capital_policy_requests",
             "uncertainty_resolution_requests", "credential_rotation_requests")
_LEGACY = ("user_profiles", "user_configs", "api_keys")

_WEBAUTH_HERE = f"""
SELECT count(*) FROM pg_shdepend d
WHERE d.refclassid = 'pg_authid'::regclass
  AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}')
  AND d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
"""


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _build_before(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{WEBAUTH}') "
            f"THEN CREATE ROLE {WEBAUTH} LOGIN; END IF; END $$")
    engine.dispose()
    alembic(url, "upgrade", _BEFORE)
    stamp_realm(url, "ci")
    engine = create_engine(url)
    with engine.begin() as conn:
        # fresh-host-setup §1c, run as the owner after the migrations.
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA auth TO {WEBAUTH}")
        conn.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO {WEBAUTH}")
        conn.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO {WEBAUTH}")
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA auth "
                             f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {WEBAUTH}")
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA auth "
                             f"GRANT USAGE, SELECT ON SEQUENCES TO {WEBAUTH}")
    engine.dispose()


def _seed(conn: Connection) -> None:
    conn.execute(text("INSERT INTO exchange_accounts(id, venue, label, lifecycle_status) "
                      "VALUES (:a, 'bitfinex', 'x', 'active')"), {"a": _ACCOUNT})
    _member(conn, _NODE, "owner")
    _member(conn, _OLD, "operator")
    conn.execute(text(
        'INSERT INTO auth."user" (id, name, email, "emailVerified", "createdAt", "updatedAt", '
        'role, banned, "twoFactorEnabled") VALUES (:u, :u, :e, true, now(), now(), '
        "'admin', false, true)"), {"u": _OLD, "e": f"{_OLD}@test.invalid"})
    conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": _OLD})
    conn.execute(text(
        "INSERT INTO user_configs (user_id, exchange_account_id, config) "
        "VALUES (:u, :a, '{}')"), {"u": _OLD, "a": _ACCOUNT})
    conn.execute(text(
        "INSERT INTO api_keys (user_id, exchange_account_id, api_key, secret_ciphertext, "
        "secret_nonce, wrapped_dek, dek_nonce) VALUES (:u, :a, 'k', '\\x00', '\\x00', "
        "'\\x00', '\\x00')"), {"u": _OLD, "a": _ACCOUNT})
    # A settled request from the old identity does not count against assertion B.
    _request(conn, "trading_control_requests", _OLD, settled=True)


def _member(conn: Connection, user_id: str, role: str) -> None:
    conn.execute(text("INSERT INTO exchange_account_memberships(exchange_account_id, user_id, "
                      "role) VALUES (:a, :u, :r)"), {"a": _ACCOUNT, "u": user_id, "r": role})


def _request(conn: Connection, table: str, by: str, *, settled: bool = False) -> None:
    """One request row; FKs and triggers off, only its own constraints apply."""
    columns: dict[str, Any] = {
        "request_id": uuid4(), "exchange_account_id": _ACCOUNT,
        "deployment_environment": "ci", "requested_by": by, "created_at_ms": 1,
    }
    columns |= {
        "trading_control_requests": {"action": "kill", "reason": "x"},
        "capital_policy_requests": {"symbol": "fUST", "action": "disable", "reason": "x"},
        "uncertainty_resolution_requests": {
            "uncertainty_id": uuid4(), "action": "mark_not_accepted", "observation_id": uuid4()},
        "credential_rotation_requests": {
            "recipient_id": uuid4(), "replaces_credential_id": uuid4(),
            "sealed_credential": b"\x00" * 64, "label": "x", "reason": "x"},
    }[table]
    if settled:
        columns |= {"state": "rejected", "processed_at_ms": 2, "outcome_reason": "x"}
    conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
    conn.execute(text(f"INSERT INTO {table} ({', '.join(columns)}) "
                      f"VALUES ({', '.join(f':{c}' for c in columns)})"), columns)
    conn.exec_driver_sql("SET LOCAL session_replication_role = origin")


def _state(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "revision": conn.scalar(text("SELECT version_num FROM alembic_version")),
            "auth": conn.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname='auth'")),
            "members": sorted(conn.execute(text(
                "SELECT user_id FROM exchange_account_memberships")).scalars()),
            "auth_defaults": conn.scalar(text(
                "SELECT count(*) FROM pg_default_acl WHERE defaclnamespace = "
                "(SELECT oid FROM pg_namespace WHERE nspname = 'auth')")),
            "legacy": {t: conn.scalar(text(f"SELECT count(*) FROM {t}")) for t in _LEGACY},
            "webauth_here": conn.scalar(text(_WEBAUTH_HERE)),
        }


def _webauth_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
                                {"r": WEBAUTH}))


def _refuses(engine: Engine, match: str) -> None:
    before = _state(engine)
    assert before["revision"] == _BEFORE and before["auth"] == 1
    with pytest.raises(RuntimeError, match=match):
        alembic(_url(engine), "upgrade", _RETIRE)
    assert _state(engine) == before


@pytest.fixture
def before_db(pg_templates, pg_clone) -> Iterator[Engine]:
    engine = create_engine(pg_clone(pg_templates.template(f"auth_retire_{_BEFORE}",
                                                          _build_before)))
    with engine.begin() as conn:
        _seed(conn)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_drops_auth_and_the_old_memberships(before_db) -> None:
    before = _state(before_db)
    assert before["members"] == sorted([_NODE, _OLD])
    assert before["auth_defaults"] == 2 and before["webauth_here"] > 0
    assert before["legacy"] == dict.fromkeys(_LEGACY, 1)

    alembic(_url(before_db), "upgrade", _RETIRE)

    after = _state(before_db)
    assert after == {"revision": _RETIRE, "auth": 0, "members": [_NODE], "auth_defaults": 0,
                     "legacy": before["legacy"], "webauth_here": 0}
    with before_db.begin() as conn:
        assert conn.scalar(text("SELECT public.operator_authorized(:a, :u)"),
                           {"a": _ACCOUNT, "u": _NODE}) is True
        assert conn.scalar(text("SELECT public.operator_authorized(:a, :u)"),
                           {"a": _ACCOUNT, "u": _OLD}) is False
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_user_profiles_user'")) == 0
        # The vault tables keep their own foreign keys to user_profiles.
        assert set(conn.scalars(text(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid IN ('public.api_keys'::regclass, 'public.user_configs'::regclass) "
            "AND confrelid = 'public.user_profiles'::regclass"))) == {
            "fk_api_keys_user_profile", "fk_user_configs_user_profile"}


def test_a_live_account_without_a_numeric_operator_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.execute(text("DELETE FROM exchange_account_memberships WHERE user_id = :u"),
                     {"u": _NODE})
    _refuses(before_db, "1 active or halted accounts have no owner/operator membership")


def test_a_retired_account_needs_no_numeric_operator(before_db) -> None:
    with before_db.begin() as conn:
        conn.execute(text("DELETE FROM exchange_account_memberships WHERE user_id = :u"),
                     {"u": _NODE})
        conn.execute(text("UPDATE exchange_accounts SET lifecycle_status = 'retired'"))
    alembic(_url(before_db), "upgrade", _RETIRE)
    assert _state(before_db)["members"] == []


@pytest.mark.parametrize("table", _OUTBOXES)
def test_a_pending_request_from_an_old_identity_refuses(before_db, table) -> None:
    with before_db.begin() as conn:
        _request(conn, table, _OLD)
    _refuses(before_db, "1 requested operator requests come from a non-numeric requested_by")


@pytest.mark.parametrize("table", _OUTBOXES)
def test_a_pending_request_from_the_tailnet_identity_passes(before_db, table) -> None:
    with before_db.begin() as conn:
        _request(conn, table, _NODE)
    alembic(_url(before_db), "upgrade", _RETIRE)
    assert _state(before_db)["auth"] == 0


_OUTSIDE = {
    "view": 'CREATE VIEW public.auth_users AS SELECT id FROM auth."user"',
    "function": ("CREATE FUNCTION public.auth_user_count() RETURNS bigint LANGUAGE sql "
                 'AS $$ SELECT count(*) FROM auth."user" $$'),
    "column_default": (
        "CREATE SEQUENCE auth.legacy_seq; "
        "ALTER TABLE public.user_profiles ADD COLUMN legacy_n bigint "
        "DEFAULT nextval('auth.legacy_seq')"),
    "trigger": (
        "CREATE FUNCTION auth.touch() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RETURN NEW; END $$; "
        "CREATE TRIGGER touch_profiles BEFORE UPDATE ON public.user_profiles "
        "FOR EACH ROW EXECUTE FUNCTION auth.touch()"),
    # Catalogs beyond tables and functions: CASCADE drops these without a word as well.
    "operator": (
        "CREATE FUNCTION auth.same(integer, integer) RETURNS boolean LANGUAGE sql "
        "AS $$ SELECT $1 = $2 $$; "
        "CREATE OPERATOR public.=== (LEFTARG = integer, RIGHTARG = integer, "
        "FUNCTION = auth.same)"),
    "event_trigger": (
        "CREATE FUNCTION auth.on_ddl() RETURNS event_trigger LANGUAGE plpgsql "
        "AS $$ BEGIN END $$; "
        "CREATE EVENT TRIGGER auth_on_ddl ON ddl_command_end EXECUTE FUNCTION auth.on_ddl()"),
}


@pytest.mark.parametrize("kind", sorted(_OUTSIDE))
def test_an_object_outside_auth_that_depends_on_it_refuses(before_db, kind) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql(_OUTSIDE[kind])
    match = ("function bodies outside auth read it" if kind == "function"
             else "objects outside auth depend on it")
    _refuses(before_db, match)


def test_a_second_foreign_key_into_auth_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE public.user_configs ADD CONSTRAINT fk_user_configs_auth_user "
            'FOREIGN KEY (user_id) REFERENCES auth."user"(id)')
    _refuses(before_db, "the foreign keys into auth are")


def test_a_row_lost_during_the_drop_refuses(before_db) -> None:
    """Whatever removes a legacy row while ``auth`` goes (here an event trigger on the drop)
    turns the upgrade into a refusal, not a silent loss."""
    with before_db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE FUNCTION public.lose_a_profile() RETURNS event_trigger LANGUAGE plpgsql "
            "AS $$ BEGIN DELETE FROM public.api_keys; END $$")
        conn.exec_driver_sql("CREATE EVENT TRIGGER lose_a_profile ON sql_drop "
                             "EXECUTE FUNCTION public.lose_a_profile()")
    try:
        _refuses(before_db, "legacy row counts changed")
    finally:
        with before_db.begin() as conn:
            conn.exec_driver_sql("DROP EVENT TRIGGER lose_a_profile")


# -- the role drop, on a cluster of its own --------------------------------------------------

retire_auth_pg_proc = postgresql_proc(
    executable=str((pg_local.find_pg_bin() or Path("/nonexistent-postgresql-bin")) / "pg_ctl"),
    host="127.0.0.1", port=None, user="test", password="test", dbname="test",
    postgres_options=pg_local.SERVER_OPTIONS,
)


def _admin(proc: Any, database: str = "postgres") -> psycopg.Connection[Any]:
    return psycopg.connect(host=proc.host, port=proc.port, user=proc.user,
                           password=proc.password, dbname=database, autocommit=True)


@pytest.fixture
def own_cluster_db(retire_auth_pg_proc) -> Iterator[Engine]:
    """One application database on a cluster nothing else uses, as in production."""
    with _admin(retire_auth_pg_proc) as conn:
        conn.execute("DROP DATABASE IF EXISTS retire")
        conn.execute("DROP DATABASE IF EXISTS retire_other")
        for role in (WEBAUTH, "bfx_bot", "bfx_webapi", "bfx_cutover_reader"):
            conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute("CREATE DATABASE retire")
    url = (f"postgresql+psycopg://{retire_auth_pg_proc.user}:{retire_auth_pg_proc.password}"
           f"@{retire_auth_pg_proc.host}:{retire_auth_pg_proc.port}/retire")
    _build_before(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)
    try:
        yield engine
    finally:
        engine.dispose()


def test_on_its_own_cluster_the_role_is_dropped(own_cluster_db) -> None:
    assert _webauth_exists(own_cluster_db)
    alembic(_url(own_cluster_db), "upgrade", _RETIRE)
    assert not _webauth_exists(own_cluster_db)
    assert _state(own_cluster_db)["members"] == [_NODE]


def test_a_role_another_database_still_grants_stays_and_says_where(
    own_cluster_db, retire_auth_pg_proc,
) -> None:
    with _admin(retire_auth_pg_proc) as conn:
        conn.execute("CREATE DATABASE retire_other")
    with _admin(retire_auth_pg_proc, "retire_other") as conn:
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {WEBAUTH}")
    notices: list[str] = []

    def listen(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.add_notice_handler(lambda diag: notices.append(diag.message_primary))

    event.listen(Pool, "connect", listen)
    try:
        alembic(_url(own_cluster_db), "upgrade", _RETIRE)
    finally:
        event.remove(Pool, "connect", listen)
    assert _webauth_exists(own_cluster_db) and _state(own_cluster_db)["webauth_here"] == 0
    with own_cluster_db.connect() as conn:
        assert conn.execute(text("SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid "
                                 "WHERE rolname = :r"), {"r": WEBAUTH}).one() == (False, True)
    assert f"{WEBAUTH} kept: still referenced by retire_other" in notices


def test_a_database_without_the_role_upgrades(retire_auth_pg_proc) -> None:
    """A cluster that never had ``bfx_webauth`` (the plain build): the step is skipped."""
    from tests.pg_templates import template_at

    with _admin(retire_auth_pg_proc) as conn:
        conn.execute("DROP DATABASE IF EXISTS retire")
        conn.execute("DROP DATABASE IF EXISTS retire_other")
        conn.execute(f"DROP ROLE IF EXISTS {WEBAUTH}")
        conn.execute("CREATE DATABASE retire")
    url = (f"postgresql+psycopg://{retire_auth_pg_proc.user}:{retire_auth_pg_proc.password}"
           f"@{retire_auth_pg_proc.host}:{retire_auth_pg_proc.port}/retire")
    template_at(_BEFORE)(url)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _seed(conn)
        # To head: a later migration that names bfx_webauth would fail here, as in production.
        alembic(url, "upgrade", "head")
        assert _state(engine)["auth"] == 0 and _state(engine)["members"] == [_NODE]
    finally:
        engine.dispose()
