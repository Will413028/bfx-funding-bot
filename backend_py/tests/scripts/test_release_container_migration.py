"""Actual immutable candidate UV migration and restricted bot startup receipts."""
import asyncio
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from scripts.release_package import run


@pytest.mark.integration
async def test_image_migration_then_restricted_runtime_check_without_sync_or_policy_seed(pg_engine, pg_container, tmp_path):
    # Test-owned PG only; actual migration, not ORM create_all, owns this schema.
    async with pg_engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$"))
        await connection.execute(text("ALTER ROLE bfx_bot LOGIN PASSWORD 'synthetic' NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"))
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO bfx_bot"))
        await connection.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO bfx_bot"))
        await connection.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO bfx_bot"))
    image = run(["docker", "build", "-q", "--platform", "linux/arm64", str(Path(__file__).resolve().parents[2])]).decode().strip()
    network = "task5-migration-" + uuid4().hex
    pg_id = pg_container.get_wrapped_container().id
    run(["docker", "network", "create", "--internal", network])
    run(["docker", "network", "connect", "--alias", "fixture-db", network, pg_id])
    account = uuid4()
    async def command(principal, args):
        return await asyncio.to_thread(subprocess.run, ["docker", "run", "--rm", "--pull=never", "--read-only",
            "--network", network, "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--env", f"DATABASE_URL=postgresql://{principal}@fixture-db:5432/test",
            "--env", "UV_NO_SYNC=1", "--env", "UV_CACHE_DIR=/tmp/uv",
            "--env", f"BFX_EXCHANGE_ACCOUNT_ID={account}", "--env", "BFX_DEPLOYMENT_ENV=ci",
            image, *args], capture_output=True, text=True)
    try:
        before = await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "schema"])
        assert before.returncode == 0, before.stderr
        initial = json.loads(before.stdout)
        assert initial["schema_heads"] == []
        assert initial["database"] == "test"
        assert initial["system_identifier"].isdigit()
        migrated = await command("test:test", ["uv", "run", "alembic", "upgrade", "head"])
        assert migrated.returncode == 0, migrated.stderr
        schema = await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "schema"])
        assert schema.returncode == 0, schema.stderr
        assert json.loads(schema.stdout)["schema_heads"] == ["b4e6f8a0c203"]
        owner = await command("test:test", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
        assert owner.returncode == 2
        url = pg_container.get_connection_url().replace("+psycopg2", "")
        engine = make_async_engine_from_url(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                session.add(ExchangeAccount(id=account, venue="bitfinex", label="migration-fixture"))
            halt = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
            epoch = await halt.set_halted(True, reason="fixture", actor="fixture")
            missing = await command("bfx_bot:synthetic", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
            assert missing.returncode == 2
            repo = CapitalRepository(account_id=account, environment="ci", max_snapshot_age_ms=300000)
            async with factory.begin() as session:
                assert await session.scalar(text("SELECT count(*) FROM capital_policy_revisions")) == 0
                for symbol in ("fUST", "fUSD"):
                    await repo.apply_policy(session, symbol=symbol, expected_revision=0,
                        policy=CapitalPolicy(enabled=symbol == "fUST"), source={"fixture": True})
            ready = await command("bfx_bot:synthetic", ["/app/.venv/bin/python", "-m", "scripts.release_database", "startup"])
            assert ready.returncode == 0, ready.stderr
            assert json.loads(ready.stdout)["principal"] == "bfx_bot"
            assert json.loads(ready.stdout)["halt_id"] == epoch.id
            assert (await halt.current()).id == epoch.id
        finally:
            await engine.dispose()
    finally:
        run(["docker", "network", "disconnect", network, pg_id])
        run(["docker", "network", "rm", network])
