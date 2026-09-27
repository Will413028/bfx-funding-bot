"""funding_interest_payments on real PostgreSQL: the grants the bot and web API run under."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
ACCOUNT = "35efed2d-3004-4941-a161-ca025d9c4d53"
ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")
INSERT = ("INSERT INTO funding_interest_payments (exchange_account_id, ledger_id, "
          "deployment_environment, currency, mts, amount, balance, description) VALUES "
          f"('{ACCOUNT}', 10578187002, 'live', 'UST', 1790472624000, 0.0518895, 395.56843927, "
          "'Margin Funding Payment on wallet funding') ON CONFLICT DO NOTHING")


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(["uv", "run", "alembic", *args], cwd=BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def migrated(pg_container: Any) -> Any:
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    with engine.begin() as conn:
        for schema in ("projection_audit", "auth", "release_archive"):
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        # Worst case on the VM: default privileges already hand runtime roles ALL.
        for role in ROLES:
            conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 f"rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
    try:
        _alembic(url, "upgrade", "head")
        yield url, engine
    finally:
        with engine.begin() as conn:
            for role in ROLES:
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
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
    _alembic(url, "downgrade", "-1")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.funding_interest_payments')")) is None
    _alembic(url, "upgrade", "head")
