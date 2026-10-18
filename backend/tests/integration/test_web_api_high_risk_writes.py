"""The web API loses its high-risk writes (9c-2b) on real PostgreSQL roles.

Resume and enable may be queued only by the table owner (migrations, owner scripts) and
``bfx_operator`` (the CLI): a trigger with an allowlist, so any other role holding the
request columns' INSERT -- ``bfx_webapi`` today, one added later -- is refused. Kill and
disable stay with the web API. Adjudications and credential writes are taken away from the
web API by REVOKE, and an active credential always names its Bitfinex user (a CHECK), which
the migration asserts of the existing rows before adding it.

The trigger and a missing grant both raise SQLSTATE 42501, so the refusals below match the
trigger's message, not the error code.

Mutations (one at a time; revert after each), from the 9c-2b implementation plan:

* drop the ``trading_control_requests`` trigger: ``test_the_web_api_may_kill_but_not_resume``
  fails;
* make the trigger refuse only ``bfx_webapi``: ``test_the_trigger_is_an_allowlist`` fails;
* drop the ``capital_policy_requests`` trigger: ``test_the_web_api_may_disable_but_not_enable``
  fails;
* keep the web API's UPDATE (or its adjudication INSERT): ``test_the_web_api_cannot_write_
  credentials_or_adjudications`` fails, and so does the privilege allowlist;
* drop the CHECK: ``test_an_active_credential_always_names_its_user`` fails;
* drop the migration's pre-assertion: ``test_the_migration_refuses_an_active_credential_
  without_a_user_id`` fails (the upgrade fails at the CHECK, with another message).
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_BEFORE = "1d34a3306e02"
_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_TRIGGER_MESSAGE = "may be requested only through the operator CLI"
_WRITER = "bfx_test_writer"
_CREDENTIALS = "exchange_account_credentials"
_ADJUDICATIONS = "uncertainty_resolution_requests"


def _prepare(url: str) -> None:
    """The shared worst-case default privileges, plus the operator role (created per test
    module, as the operator allowlist test does)."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                             "rolname='bfx_operator') THEN CREATE ROLE bfx_operator; END IF; END $$")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_operator")
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES "
                             "TO bfx_operator")
    engine.dispose()


def _build_head(url: str) -> None:
    _prepare(url)
    alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


def _build_before(url: str) -> None:
    _prepare(url)
    alembic(url, "upgrade", _BEFORE)
    stamp_realm(url, "ci")


def _with_account(url: str):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) "
                          "VALUES (:a, 'bitfinex', 'x')"), {"a": _ACCOUNT})
    return engine


@pytest.fixture
def head(pg_templates, pg_clone):
    engine = _with_account(pg_clone(pg_templates.template("web_api_high_risk_head", _build_head)))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def before(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("web_api_high_risk_before", _build_before))
    engine = _with_account(url)
    try:
        yield url, engine
    finally:
        engine.dispose()


def _trading(request_id: int, action: str) -> str:
    return (f"INSERT INTO trading_control_requests ({', '.join(TradingControlRequestRow.REQUEST_COLUMNS)}) "
            f"VALUES ('00000000-0000-0000-0000-{request_id:012d}', '{_ACCOUNT}', 'ci', '{action}', "
            "'x', 'operator', 1)")


def _currency(request_id: int, action: str) -> str:
    return (f"INSERT INTO capital_policy_requests ({', '.join(CapitalPolicyRequestRow.REQUEST_COLUMNS)}) "
            f"VALUES ('00000000-0000-0000-0000-{request_id:012d}', '{_ACCOUNT}', 'ci', 'fUST', "
            f"'{action}', 'x', 'operator', 1)")


def _as(engine, role: str, sql: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        conn.exec_driver_sql(sql)


def _refused(engine, role: str, sql: str, message: str) -> None:
    with pytest.raises(Exception, match=message):
        _as(engine, role, sql)


def _has(engine, sql: str, **params) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text(sql), params))


def test_the_web_api_may_kill_but_not_resume(head) -> None:
    _refused(head, "bfx_webapi", _trading(1, "resume"), _TRIGGER_MESSAGE)
    _as(head, "bfx_webapi", _trading(2, "kill"))


def test_the_web_api_may_disable_but_not_enable(head) -> None:
    _refused(head, "bfx_webapi", _currency(1, "enable"), _TRIGGER_MESSAGE)
    _as(head, "bfx_webapi", _currency(2, "disable"))


def test_the_operator_cli_may_resume_and_enable(head) -> None:
    _as(head, "bfx_operator", _trading(1, "resume"))
    _as(head, "bfx_operator", _currency(2, "enable"))


def test_the_owner_may_resume_and_enable(head) -> None:
    with head.begin() as conn:
        conn.exec_driver_sql(_trading(1, "resume"))
        conn.exec_driver_sql(_currency(2, "enable"))


def test_the_trigger_is_an_allowlist(head) -> None:
    """A role that is neither the owner nor ``bfx_operator``, with the web API's kill and
    disable grants, is refused resume and enable too: not only ``bfx_webapi`` is."""
    with head.begin() as conn:
        conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                             f"rolname='{_WRITER}') THEN CREATE ROLE {_WRITER}; END IF; END $$")
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {_WRITER}")
        conn.exec_driver_sql(f"GRANT INSERT ({', '.join(TradingControlRequestRow.REQUEST_COLUMNS)}) "
                             f"ON public.trading_control_requests TO {_WRITER}")
        conn.exec_driver_sql(f"GRANT INSERT ({', '.join(CapitalPolicyRequestRow.REQUEST_COLUMNS)}) "
                             f"ON public.capital_policy_requests TO {_WRITER}")
    _refused(head, _WRITER, _trading(1, "resume"), _TRIGGER_MESSAGE)
    _refused(head, _WRITER, _currency(2, "enable"), _TRIGGER_MESSAGE)
    _as(head, _WRITER, _trading(3, "kill"))
    _as(head, _WRITER, _currency(4, "disable"))


def _credential(conn, status: str, user_id: int | None) -> UUID:
    verified = "now()" if status == "active" else "NULL"
    return conn.scalar(text(
        f"INSERT INTO {_CREDENTIALS} (exchange_account_id, venue, api_key, secret_ciphertext, "
        "secret_nonce, wrapped_dek, dek_nonce, lifecycle_status, verified_at, bitfinex_user_id) "
        f"VALUES (:a, 'bitfinex', :k, '\\x00', '\\x00', '\\x00', '\\x00', :s, {verified}, :u) "
        "RETURNING id"), {"a": _ACCOUNT, "k": f"key-{status}-{user_id}", "s": status, "u": user_id})


def test_the_web_api_cannot_write_credentials_or_adjudications(head) -> None:
    with head.begin() as conn:
        _credential(conn, "active", 4242)
    _refused(head, "bfx_webapi",
             f"INSERT INTO {_ADJUDICATIONS} (request_id, exchange_account_id, "
             "deployment_environment, uncertainty_id, action, requested_by, created_at_ms, "
             f"observation_id) VALUES (gen_random_uuid(), '{_ACCOUNT}', 'ci', gen_random_uuid(), "
             "'mark_not_accepted', 'operator', 1, gen_random_uuid())", "permission denied")
    _refused(head, "bfx_webapi",
             f"INSERT INTO {_CREDENTIALS} (exchange_account_id, venue, api_key, secret_ciphertext, "
             "secret_nonce, wrapped_dek, dek_nonce) "
             f"VALUES ('{_ACCOUNT}', 'bitfinex', 'k2', '\\x00', '\\x00', '\\x00', '\\x00')",
             "permission denied")
    _refused(head, "bfx_webapi", f"UPDATE {_CREDENTIALS} SET lifecycle_status = 'retired'",
             "permission denied")
    _refused(head, "bfx_webapi", f"UPDATE {_CREDENTIALS} SET bitfinex_user_id = 1",
             "permission denied")
    with head.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        assert conn.scalar(text(f"SELECT count(*) FROM {_CREDENTIALS}")) == 1
    for table in (_CREDENTIALS, _ADJUDICATIONS):
        for privilege in ("INSERT", "UPDATE"):
            assert not _has(head, "SELECT has_any_column_privilege('bfx_webapi', :t, :p)",
                            t=f"public.{table}", p=privilege), (table, privilege)


def test_an_active_credential_always_names_its_user(head) -> None:
    with head.begin() as conn:
        active = _credential(conn, "active", 4242)
    with head.begin() as conn, pytest.raises(
            Exception, match="ck_exchange_account_credentials_active_user_id"):
        conn.execute(text(f"UPDATE {_CREDENTIALS} SET bitfinex_user_id = NULL WHERE id = :i"),
                     {"i": active})
    with head.begin() as conn:
        conn.execute(text(f"UPDATE {_CREDENTIALS} SET lifecycle_status = 'retired' WHERE id = :i"),
                     {"i": active})
        pending = _credential(conn, "pending", None)
    with head.begin() as conn, pytest.raises(
            Exception, match="ck_exchange_account_credentials_active_user_id"):
        conn.execute(text(f"UPDATE {_CREDENTIALS} SET lifecycle_status = 'active', "
                          "verified_at = now() WHERE id = :i"), {"i": pending})


def test_the_migration_refuses_an_active_credential_without_a_user_id(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        _credential(conn, "active", None)
    with pytest.raises(RuntimeError, match="active credential without bitfinex_user_id"):
        alembic(url, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _BEFORE
