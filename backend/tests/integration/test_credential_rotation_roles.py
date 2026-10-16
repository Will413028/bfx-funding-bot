"""1db83367918c on real PostgreSQL roles: who may touch the rotation tables.

The build hands every new table to every runtime role through default privileges (worse than
production, where only ``bfx_bot`` has them), so the migration must take back what it does
not mean to grant. The operator CLI's whole allowlist is ``test_operator_privilege_allowlist``;
the end-to-end path is ``test_credential_rotation``.

Mutation: grant ``bfx_operator`` SELECT on ``sealed_credential`` (or the whole table) in
``1db83367918c``: ``test_the_sealed_credential_is_the_bots_alone`` fails here and the allowlist
test fails too.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from bfx_funding_bot.modules.accounts.rotation_tables import CredentialRotationRequestRow
from tests.pg_templates import alembic

from .test_ledger_schema_roles import _prepare

pytestmark = pytest.mark.integration

REQUESTS = "credential_rotation_requests"
RECIPIENTS = "credential_rotation_recipients"
ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_operator")


def _build(url: str) -> None:
    _prepare(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                             "rolname='bfx_operator') THEN CREATE ROLE bfx_operator; END IF; END $$")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_operator")
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES "
                             "TO bfx_operator")
    engine.dispose()
    alembic(url, "upgrade", "head")


@pytest.fixture
def conn(pg_templates, pg_clone):
    engine = create_engine(pg_clone(pg_templates.template("credential_rotation_grants", _build)))
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _table(conn, role: str, table: str, privilege: str) -> bool:
    return bool(conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": f"public.{table}", "p": privilege}))


def _column(conn, role: str, table: str, column: str, privilege: str) -> bool:
    return bool(conn.scalar(text("SELECT has_column_privilege(:r, :t, :c, :p)"),
                            {"r": role, "t": f"public.{table}", "c": column, "p": privilege}))


def _any_column(conn, role: str, table: str, privilege: str) -> bool:
    return bool(conn.scalar(text("SELECT has_any_column_privilege(:r, :t, :p)"),
                            {"r": role, "t": f"public.{table}", "p": privilege}))


def test_no_role_may_delete_or_truncate_either_table(conn) -> None:
    for role in ROLES:
        for table in (REQUESTS, RECIPIENTS):
            for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not _table(conn, role, table, privilege), (role, table, privilege)


def test_the_web_roles_have_nothing(conn) -> None:
    for role in ("bfx_webapi", "bfx_webauth"):
        for table in (REQUESTS, RECIPIENTS):
            for privilege in ("SELECT", "INSERT", "UPDATE"):
                assert not _any_column(conn, role, table, privilege), (role, table, privilege)


def test_the_bot_records_outcomes_and_publishes_recipients(conn) -> None:
    assert _table(conn, "bfx_bot", REQUESTS, "SELECT")
    assert not _any_column(conn, "bfx_bot", REQUESTS, "INSERT")
    for column in CredentialRotationRequestRow.REQUEST_COLUMNS:
        assert not _column(conn, "bfx_bot", REQUESTS, column, "UPDATE"), column
    for column in CredentialRotationRequestRow.WORKER_COLUMNS:
        assert _column(conn, "bfx_bot", REQUESTS, column, "UPDATE"), column
    assert _table(conn, "bfx_bot", RECIPIENTS, "SELECT")
    assert _table(conn, "bfx_bot", RECIPIENTS, "INSERT")
    assert not _any_column(conn, "bfx_bot", RECIPIENTS, "UPDATE")


def test_the_operator_queues_request_columns_only(conn) -> None:
    for column in CredentialRotationRequestRow.REQUEST_COLUMNS:
        assert _column(conn, "bfx_operator", REQUESTS, column, "INSERT"), column
    for column in CredentialRotationRequestRow.WORKER_COLUMNS:
        assert not _column(conn, "bfx_operator", REQUESTS, column, "INSERT"), column
    for table in (REQUESTS, RECIPIENTS):
        assert not _any_column(conn, "bfx_operator", table, "UPDATE"), table
    assert _table(conn, "bfx_operator", RECIPIENTS, "SELECT")
    assert not _any_column(conn, "bfx_operator", RECIPIENTS, "INSERT")


def test_the_sealed_credential_is_the_bots_alone(conn) -> None:
    for column in (*CredentialRotationRequestRow.REQUEST_COLUMNS,
                   *CredentialRotationRequestRow.WORKER_COLUMNS):
        readable = _column(conn, "bfx_operator", REQUESTS, column, "SELECT")
        assert readable is (column != "sealed_credential"), column
    assert _column(conn, "bfx_bot", REQUESTS, "sealed_credential", "SELECT")
    # Nor any credential secret: only the identity and lifecycle columns the CLI names.
    for column in ("api_key", "secret_ciphertext", "secret_nonce", "wrapped_dek", "dek_nonce"):
        assert not _column(conn, "bfx_operator", "exchange_account_credentials", column,
                           "SELECT"), column
    assert _column(conn, "bfx_operator", "exchange_account_credentials", "operator_request_id",
                   "SELECT")
