"""funding_interest_payments on real PostgreSQL: the grants the bot and web API run under."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import alembic as _alembic
from tests.pg_templates import stamp_realm

from .test_ledger_schema_roles import pre_switch_url

pytestmark = pytest.mark.integration

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")
INSERT = ("INSERT INTO funding_interest_payments (exchange_account_id, ledger_id, "
          "deployment_environment, currency, mts, amount, balance, description) VALUES "
          f"('{ACCOUNT}', 10578187002, 'ci', 'UST', 1790472624000, 0.0518895, 395.56843927, "
          "'Margin Funding Payment on wallet funding') ON CONFLICT DO NOTHING")


def _build_migrated(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        # Worst case on the VM: default privileges already hand runtime roles ALL.
        for role in ROLES:
            conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 f"rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
    engine.dispose()
    _alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


@pytest.fixture
def migrated(pg_templates: Any, pg_clone: Any) -> Any:
    """A fresh copy of the upgraded database; the upgrade runs once per session."""
    url = pg_clone(pg_templates.template("interest_payments_migrated", _build_migrated))
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()


def _can(conn: Any, role: str, privilege: str) -> bool:
    return bool(conn.scalar(text("SELECT has_table_privilege(:r, 'funding_interest_payments', :p)"),
                            {"r": role, "p": privilege}))


def test_bot_appends_idempotently_and_web_api_only_reads(migrated: Any) -> None:
    _url, engine = migrated
    with engine.connect() as conn:
        assert [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if _can(conn, "bfx_bot", p)] == ["SELECT", "INSERT"]
        assert [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if _can(conn, "bfx_webapi", p)] == ["SELECT"]
        assert not any(_can(conn, "bfx_webauth", p) for p in ("SELECT", "INSERT"))
    for _ in range(2):  # the second sync re-reads a day of overlap
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
            conn.exec_driver_sql(INSERT)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM funding_interest_payments")) == 1
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(INSERT)


def test_migration_is_reversible_and_leaves_no_drift(migrated: Any) -> None:
    url, engine = migrated
    _alembic(url, "check")
    pre_switch_url(url)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    _alembic(url, "downgrade", "5b9e3d7a2f41")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.funding_interest_payments')")) is None
    _alembic(url, "upgrade", "head")
