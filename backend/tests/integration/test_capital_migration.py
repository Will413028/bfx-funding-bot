"""Migration contract on an isolated PostgreSQL container, including runtime ACLs."""
import asyncio

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.pg_templates import alembic, stamp_realm

pytestmark = pytest.mark.integration


def test_capital_upgrade_drift_and_immutable_runtime_evidence(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        connection.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$")
        connection.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_bot")
        connection.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_bot")
        connection.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO bfx_bot")
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    stamp_realm(url, "ci")
    with engine.begin() as connection:
        assert "capital_snapshots" in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT count(*) FROM capital_policy_revisions")) == 0
        assert connection.scalar(text("SELECT has_table_privilege('bfx_bot','capital_snapshots','INSERT')"))
        assert not connection.scalar(text("SELECT has_table_privilege('bfx_bot','capital_snapshots','UPDATE')"))
        assert not connection.scalar(text("SELECT has_table_privilege('bfx_bot','capital_policy_heads','UPDATE')"))
        connection.exec_driver_sql("INSERT INTO exchange_accounts (id,venue,label) VALUES ('00000000-0000-0000-0000-00000000ca01','bitfinex','migration')")
        connection.exec_driver_sql("""INSERT INTO capital_snapshot_queries
            (id,exchange_account_id,deployment_environment,command_fence,query_revision,started_at_ms)
            VALUES ('00000000-0000-0000-0000-00000000ca02','00000000-0000-0000-0000-00000000ca01','ci',0,1,1000)""")
    for sql in ("UPDATE capital_snapshot_queries SET command_fence=5",
                "DELETE FROM capital_snapshot_queries", "TRUNCATE capital_snapshot_queries CASCADE"):
        with engine.begin() as connection, pytest.raises(Exception, match="immutable capital"):
            connection.exec_driver_sql(sql)
    asyncio.run(_migrated_runtime_roundtrip(url))
    for sql in ("UPDATE capital_policy_revisions SET digest='forged'",
                "DELETE FROM capital_snapshots", "UPDATE event_log SET occurred_at_ms=0"):
        with engine.begin() as connection, pytest.raises(Exception, match="immutable capital"):
            connection.exec_driver_sql(sql)
    engine.dispose()


async def _migrated_runtime_roundtrip(url):
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from tests.integration.test_capital_repository import (
        authorize,
        intent,
        repository,
        setup_policy,
        simulated_guard,
        snapshot,
    )

    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="migrated-runtime"))
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    # New migration must pass the real writer allowlist and restricted runtime
    # must retain only the operational reads/inserts it already inherited.
    event, decision = intent(account)
    async with factory.begin() as session:
        await session.execute(text("SET LOCAL ROLE bfx_bot"))
        await repo.authorize_and_append_intent(session, intent=event, decision=decision,
            expected_revision=policy.revision, expected_digest=policy.digest,
            expected_snapshot_seq=seq, now_ms=1100, locked_guard=simulated_guard)
    async with factory.begin() as session:
        await session.execute(text("UPDATE alembic_version SET version_num='unknown-capital-head'"))
    try:
        with pytest.raises(ValueError, match="migration incomplete"):
            await authorize(factory, repo, policy, seq, cid=2)
    finally:
        async with factory.begin() as session:
            await session.execute(text("UPDATE alembic_version SET version_num='a9d3e5f7b102'"))
        await engine.dispose()
