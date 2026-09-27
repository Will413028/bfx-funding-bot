from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.accounts.identity_cutover import IdentityCutover, IdentityManifest
from tests.pg_templates import alembic

pytestmark = pytest.mark.integration

_ADDITIVE = "8a1b2c3d4e5f"


def _upgrade(sync_url: str, revision: str) -> None:
    alembic(sync_url, "upgrade", revision)


@pytest.fixture
def additive_url(pg_templates, pg_clone) -> str:
    """A fresh database migrated from empty to the additive identity revision."""
    def build(url: str) -> None:
        _upgrade(url, _ADDITIVE)

    return pg_clone(pg_templates.template(f"identity_additive_{_ADDITIVE}", build))


async def test_identity_revision_creates_tables_and_nullable_columns(additive_url) -> None:
    """Run the additive revision against a clean PostgreSQL database."""
    sync_url = additive_url

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


async def test_cutover_preflight_runs_at_additive_revision_before_event_v3(additive_url) -> None:
    """Halt 1 must not select columns introduced only after its contract revision."""
    sync_url = additive_url
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO event_log
                    (account_id, deployment_environment, event_type, payload, occurred_at_ms)
                VALUES ('primary', 'prod', 'TEST', '{"amount":"1"}'::jsonb, 1)
            """))
    finally:
        engine.dispose()

    manifest = IdentityManifest.from_dict({
        "version": 1,
        "accounts": [{
            "exchange_account_id": "550e8400-e29b-41d4-a716-446655440000",
            "venue": "bitfinex",
            "label": "Primary",
            "legacy_realms": ["primary"],
            "user_ids": [],
            "memberships": {},
        }],
    })
    async_engine = create_async_engine(sync_url.replace("+psycopg", "+asyncpg"))
    try:
        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        async with factory() as session:
            report = await IdentityCutover(kek=bytes(range(32))).preflight(session, manifest)
    finally:
        await async_engine.dispose()

    assert report.event_head == 1
    assert report.unmapped_rows == ()
    assert report.legacy_realm_counts == {"primary": 1}


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
    additive_url, seed_sql: str, expected: str
) -> None:
    sync_url = additive_url

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


async def test_contract_revision_enforces_uuid_scope_and_removes_empty_legacy_tables(additive_url) -> None:
    sync_url = additive_url

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
