"""build_daemon refuses what no composition covers: ``paper``, and a simulated boot on ``legacy``.

``paper`` ran an echo executor that is gone: it is refused in ``load_config`` with a pointer to
``shadow``. ``shadow`` runs on the simulated venue, which supports only the ``ledger`` authority: a shadow
boot on a ``legacy`` epoch stops after the authority read, before the vault, any client or
any worker exists.

Mutations (one at a time; revert after each):

* accept ``BFX_PHASE=paper`` in ``load_config``: ``test_paper_boot_is_refused_naming_shadow`` fails.
* the simulated venue accepts the ``legacy`` epoch: ``test_a_simulated_boot_on_a_legacy_epoch_is_refused`` fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.apps.bot import build_daemon
from bfx_funding_bot.core.authority import AuthorityMismatch
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
async def test_a_simulated_boot_on_a_legacy_epoch_is_refused(
    monkeypatch, tmp_path, httpx_mock, realm,
) -> None:
    engine = await _database(
        monkeypatch, tmp_path, BFX_PHASE="shadow", BFX_DEPLOYMENT_ENV=realm,
    )
    # The database is stamped for the realm this process runs as: the realm check passes
    # and the refusal is the authority one.
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE database_realm SET realm = :realm"), {"realm": realm})
        # A database whose latest epoch is legacy again (restored from before the switch).
        await conn.execute(text(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "VALUES (3, 'legacy', 3, 'test', 'restored')"))
    try:
        with pytest.raises(AuthorityMismatch, match=r"authority_unsupported value=legacy build=ledger"):
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
