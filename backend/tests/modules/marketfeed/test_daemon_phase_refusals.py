"""build_daemon refuses the phases that have no composition in this build.

``paper`` ran an echo executor that is gone: it is refused in ``load_config`` with a pointer to
``shadow``. ``shadow`` runs on the simulated venue, which nothing composes yet: a shadow boot
stops after the database realm check, before the vault, any client or any worker exists.

Mutations (one at a time; revert after each):

* accept ``BFX_PHASE=paper`` in ``load_config``: ``test_paper_boot_is_refused_naming_shadow`` fails.
* let a ``shadow`` boot proceed past the realm check: ``test_shadow_boot_is_refused_until_the_simulated_venue_is_composed`` fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.apps.bot import build_daemon
from bfx_funding_bot.core.errors import ConfigurationError
from tests.modules.marketfeed.account_test_helpers import (
    live_construction_env,
    seed_exchange_account,
    write_cells_yaml,
)


async def _database(monkeypatch, tmp_path: Path, **env: str) -> AsyncEngine:
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    url = f"sqlite+aiosqlite:///{tmp_path / 'phase.db'}"
    live_construction_env(monkeypatch, url, **env)
    engine = make_async_engine_from_url(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", ["ci", "shadow"])
async def test_shadow_boot_is_refused_until_the_simulated_venue_is_composed(
    monkeypatch, tmp_path, httpx_mock, realm,
) -> None:
    engine = await _database(
        monkeypatch, tmp_path, BFX_PHASE="shadow", BFX_DEPLOYMENT_ENV=realm,
    )
    # The database is stamped for the realm this process runs as: the realm check passes
    # and the refusal is the shadow one, not a mismatch.
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE database_realm SET realm = :realm"), {"realm": realm})
    try:
        with pytest.raises(ConfigurationError, match=r"shadow runs on the simulated venue.*P2b"):
            await build_daemon(cells_yaml_path=write_cells_yaml(tmp_path), skip_ws=True)
        # Nothing reached any venue, public or authenticated.
        assert httpx_mock.get_requests() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_paper_boot_is_refused_naming_shadow(monkeypatch, tmp_path, httpx_mock) -> None:
    engine = await _database(monkeypatch, tmp_path, BFX_PHASE="paper")
    try:
        with pytest.raises(ValueError, match=r"BFX_PHASE=paper was removed; use BFX_PHASE=shadow"):
            await build_daemon(cells_yaml_path=write_cells_yaml(tmp_path), skip_ws=True)
        assert httpx_mock.get_requests() == []
    finally:
        await engine.dispose()
