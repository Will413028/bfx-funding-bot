"""Actual immutable candidate UV migration and restricted bot startup receipts."""
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.release_worker import RELEASE_SCHEMA_HEAD
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from scripts.release_package import PackagingBlocked, run, run_one_shot


@pytest.mark.integration
async def test_image_migration_then_restricted_runtime_check_without_sync_or_policy_seed(
    pg_engine, pg_container, tmp_path, unapproved_release_image,
):
    # Test-owned PG only; actual migration, not ORM create_all, owns this schema.
    async with pg_engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$"))
        await connection.execute(text("ALTER ROLE bfx_bot LOGIN PASSWORD 'synthetic' NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"))
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO bfx_bot"))
        await connection.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO bfx_bot"))
        await connection.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO bfx_bot"))
    image, identity = unapproved_release_image
    from scripts.image_artifact import resolve_image
    assert resolve_image(identity, run) == image
    network = "task5-migration-" + uuid4().hex
    pg_id = pg_container.get_wrapped_container().id
    run(["docker", "network", "create", "--internal", network])
    run(["docker", "network", "connect", "--alias", "fixture-db", network, pg_id])
    account = uuid4()
    async def command(principal, args):
        env = tmp_path / "synthetic.env"
        env.write_text(f"DATABASE_URL=postgresql://{principal}@fixture-db:5432/test\n"
            f"BFX_EXCHANGE_ACCOUNT_ID={account}\nBFX_DEPLOYMENT_ENV=ci\n")
        return await asyncio.to_thread(run_one_shot, identity, env=env, network=network,
            extra_env={"UV_NO_SYNC": "1", "UV_CACHE_DIR": "/tmp/uv"}, command=args)
    try:
        before = await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "schema"])
        initial = json.loads(before)
        assert initial["schema_heads"] == []
        assert initial["database"] == "test"
        assert initial["system_identifier"].isdigit()
        await command("test:test", ["uv", "run", "alembic", "upgrade", "head"])
        schema = await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "schema"])
        assert json.loads(schema)["schema_heads"] == [RELEASE_SCHEMA_HEAD]
        with pytest.raises(PackagingBlocked, match="one_shot_exit_nonzero:2"):
            await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
        url = pg_container.get_connection_url().replace("+psycopg2", "")
        engine = make_async_engine_from_url(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                session.add(ExchangeAccount(id=account, venue="bitfinex", label="migration-fixture"))
            halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
            epoch = (await halt.transition("HALTED", cause="operator", reason="fixture",
                                           actor="fixture")).state
            with pytest.raises(PackagingBlocked, match="one_shot_exit_nonzero:2"):
                await command("bfx_bot:synthetic", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
            repo = CapitalRepository(account_id=account, environment="ci", max_snapshot_age_ms=300000)
            async with factory.begin() as session:
                assert await session.scalar(text("SELECT count(*) FROM capital_policy_revisions")) == 0
                for symbol in ("fUST", "fUSD"):
                    await repo.apply_policy(session, symbol=symbol, expected_revision=0,
                        policy=CapitalPolicy(enabled=symbol == "fUST"), source={"fixture": True})
            ready = await command("bfx_bot:synthetic", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
            assert json.loads(ready)["principal"] == "bfx_bot"
            assert json.loads(ready)["trading_state_id"] == epoch.id
            assert (await halt.current()).id == epoch.id
        finally:
            await engine.dispose()
    finally:
        run(["docker", "network", "disconnect", network, pg_id])
        run(["docker", "network", "rm", network])
