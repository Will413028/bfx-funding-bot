from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


async def test_identity_revision_creates_tables_and_nullable_columns(pg_engine, monkeypatch) -> None:
    """Run the additive revision against a clean PostgreSQL database."""
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()

    from alembic.config import Config

    from alembic import command

    command.upgrade(Config(str(_ALEMBIC_INI)), "8a1b2c3d4e5f")

    verify_engine = create_engine(sync_url)
    try:
        with verify_engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names(schema="public"))
            columns = {
                column["name"]: column
                for column in inspector.get_columns("event_log", schema="public")
            }
            fks = inspector.get_foreign_keys(
                "exchange_account_credentials", schema="public"
            )
    finally:
        verify_engine.dispose()

    assert {
        "exchange_accounts",
        "exchange_account_memberships",
        "exchange_account_credentials",
        "account_config_drafts",
        "legacy_account_realm_map",
    } <= tables
    assert columns["exchange_account_id"]["nullable"] is True
    assert any(
        fk["referred_table"] == "exchange_accounts"
        and fk.get("options", {}).get("ondelete", "").upper() == "RESTRICT"
        for fk in fks
    )
