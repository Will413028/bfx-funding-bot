"""funding_credit_history / funding_trades on real PostgreSQL: the grants the
bot and web API run under, and reversibility to a pinned revision."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import stamp_realm

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")
PARENT = "3c8f1e6a9d52"   # funding_interest_payments; the revision this one revises
TABLES = ("funding_credit_history", "funding_trades")
INSERTS = {
    "funding_credit_history": (
        "INSERT INTO funding_credit_history (exchange_account_id, kind, credit_id, "
        "deployment_environment, symbol, side, mts_create, mts_update, amount, status, rate, "
        f"period, mts_opening, mts_last_payout) VALUES ('{ACCOUNT}', 'credit', 466642176, "
        "'ci', 'fUST', 1, 1790350246000, 1790350246000, 150.76884612, 'CLOSED', 0.00019999, "
        "2, 1790350246000, 1790351088000) ON CONFLICT DO NOTHING"),
    "funding_trades": (
        "INSERT INTO funding_trades (exchange_account_id, trade_id, deployment_environment, "
        f"symbol, mts_create, offer_id, amount, rate, period, maker) VALUES ('{ACCOUNT}', "
        "432914136, 'ci', 'fUST', 1790350246000, 5123273052, 150.76884612, 0.00019999, 2, "
        "NULL) ON CONFLICT DO NOTHING"),
}


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
        stamp_realm(url, "ci")
        yield url, engine
    finally:
        with engine.begin() as conn:
            for role in ROLES:
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
        engine.dispose()


def _privileges(conn: Any, role: str, table: str) -> list[str]:
    return [p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                           {"r": role, "t": table, "p": p})]


@pytest.mark.parametrize("table", TABLES)
def test_bot_appends_idempotently_and_web_api_only_reads(migrated: Any, table: str) -> None:
    _url, engine = migrated
    with engine.connect() as conn:
        assert _privileges(conn, "bfx_bot", table) == ["SELECT", "INSERT"]
        assert _privileges(conn, "bfx_webapi", table) == ["SELECT"]
        assert _privileges(conn, "bfx_webauth", table) == []
    for _ in range(2):  # every sync re-reads an overlap
        with engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
            conn.exec_driver_sql(INSERTS[table])
    with engine.connect() as conn:
        assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(INSERTS[table])


def test_credit_history_kind_is_constrained(migrated: Any) -> None:
    _url, engine = migrated
    with engine.begin() as conn, pytest.raises(Exception, match="ck_funding_credit_history_kind"):
        conn.exec_driver_sql(INSERTS["funding_credit_history"].replace("'credit'", "'offer'"))


def test_migration_is_reversible_and_leaves_no_drift(migrated: Any) -> None:
    url, engine = migrated
    _alembic(url, "check")
    _alembic(url, "downgrade", PARENT)
    with engine.connect() as conn:
        for table in TABLES:
            assert conn.scalar(text(f"SELECT to_regclass('public.{table}')")) is None
        assert conn.scalar(text("SELECT to_regclass('public.funding_interest_payments')")) is not None
    _alembic(url, "upgrade", "head")
