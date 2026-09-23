"""config_regime — one row per daemon boot; regime boundary = restart."""
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.live_validation.regime import record_config_regime
from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow


@pytest_asyncio.fixture
async def session_factory(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


async def test_record_config_regime_inserts_row(session_factory):
    await record_config_regime(
        session_factory,
        account_id="primary",
        deployment_environment="prod",
        clamp_enabled=True,
        reprice_enabled=True,
        git_sha="abc1234",
        now_ms=1_752_100_000_000,
    )
    async with session_factory() as s:
        row = (await s.execute(select(ConfigRegimeRow))).scalar_one()
    assert (row.clamp_enabled, row.reprice_enabled) == (True, True)
    assert row.recorded_at_ms == 1_752_100_000_000
    assert row.git_sha == "abc1234"


async def test_record_config_regime_is_best_effort(session_factory):
    """Boot must not die on telemetry: same-PK double insert logs, doesn't raise."""
    for _ in range(2):
        await record_config_regime(
            session_factory,
            account_id="primary",
            deployment_environment="prod",
            clamp_enabled=False,
            reprice_enabled=False,
            git_sha=None,
            now_ms=1_752_100_000_000,
        )
    async with session_factory() as s:
        rows = (await s.execute(select(ConfigRegimeRow))).scalars().all()
    assert len(rows) == 1
