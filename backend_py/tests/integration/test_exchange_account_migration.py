from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


def _sync_url(pg_engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )


def _reset_schema(sync_url: str) -> None:
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()


def _upgrade(sync_url: str, revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config(str(_ALEMBIC_INI)), revision)


async def test_identity_revision_creates_tables_and_nullable_columns(pg_engine, monkeypatch) -> None:
    """Run the additive revision against a clean PostgreSQL database."""
    sync_url = _sync_url(pg_engine)
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_schema(sync_url)
    _upgrade(sync_url, "8a1b2c3d4e5f")

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


@pytest.mark.parametrize(
    ("seed_sql", "expected"),
    [
        (
            "INSERT INTO event_log "
            "(account_id, deployment_environment, event_type, payload, occurred_at_ms) "
            "VALUES ('legacy', 'canary', 'TEST', '{}'::jsonb, 1)",
            "NULL exchange_account_id",
        ),
        (
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (gen_random_uuid(), 'legacy@test.invalid', 'redacted')",
            "legacy tables must be empty",
        ),
    ],
)
async def test_contract_revision_refuses_incomplete_preflight(
    pg_engine, monkeypatch, seed_sql: str, expected: str
) -> None:
    sync_url = _sync_url(pg_engine)
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_schema(sync_url)
    _upgrade(sync_url, "8a1b2c3d4e5f")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO exchange_accounts (id, venue, label) "
                    "VALUES ('550e8400-e29b-41d4-a716-446655440000', 'bitfinex', 'Primary')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO legacy_account_realm_map "
                    "(realm_key, exchange_account_id, source, manifest_sha256) "
                    "VALUES ('legacy', '550e8400-e29b-41d4-a716-446655440000', 'test', 'hash')"
                )
            )
            conn.execute(text(seed_sql))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match=expected):
        _upgrade(sync_url, "9b2c3d4e5f6a")


async def test_contract_revision_enforces_uuid_scope_and_removes_empty_legacy_tables(
    pg_engine, monkeypatch
) -> None:
    sync_url = _sync_url(pg_engine)
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_schema(sync_url)
    _upgrade(sync_url, "8a1b2c3d4e5f")

    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO exchange_accounts (id, venue, label) "
                    "VALUES ('550e8400-e29b-41d4-a716-446655440000', 'bitfinex', 'Primary')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO legacy_account_realm_map "
                    "(realm_key, exchange_account_id, source, manifest_sha256) "
                    "VALUES ('legacy', '550e8400-e29b-41d4-a716-446655440000', 'test', 'hash')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO event_log "
                    "(account_id, exchange_account_id, deployment_environment, event_type, payload, occurred_at_ms) "
                    "VALUES ('legacy', '550e8400-e29b-41d4-a716-446655440000', 'canary', 'TEST', '{}'::jsonb, 1)"
                )
            )
    finally:
        engine.dispose()

    _upgrade(sync_url, "9b2c3d4e5f6a")

    verify_engine = create_engine(sync_url)
    try:
        with verify_engine.connect() as conn:
            inspector = inspect(conn)
            event_columns = {
                column["name"]: column
                for column in inspector.get_columns("event_log", schema="public")
            }
            event_fks = inspector.get_foreign_keys("event_log", schema="public")
            indexes = {
                index["name"]: index
                for index in inspector.get_indexes("event_log", schema="public")
            }
            tables = set(inspector.get_table_names(schema="public"))
    finally:
        verify_engine.dispose()

    assert event_columns["exchange_account_id"]["nullable"] is False
    assert any(
        fk["referred_table"] == "exchange_accounts"
        and fk.get("options", {}).get("ondelete", "").upper() == "RESTRICT"
        for fk in event_fks
    )
    assert indexes["idx_event_log_acct_env_seq"]["column_names"] == [
        "exchange_account_id",
        "deployment_environment",
        "event_seq",
    ]
    assert {"users", "executions", "billing_records"}.isdisjoint(tables)
