"""build_daemon refuses a database whose realm stamp is missing or is another realm's (E2).

The check runs in every phase, not only for live: a ``paper`` or ``shadow`` process pointed at
the production database, and a process running under realm ``ci`` on a prod-stamped database
(which ``apps/config.py`` alone allows), must both stop before they read anything else.

Mutations (one at a time; revert after each):

* skip ``assert_database_realm`` in ``build_daemon``: every refusal test here fails.
* run it only when ``live_executor`` is true: the paper-phase tests fail.
* compare against a constant instead of ``config.deployment_environment``: the mismatch tests fail.
* treat an empty ``database_realm`` as the process realm: the unstamped tests fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch
from tests.modules.marketfeed.account_test_helpers import (
    configure_account_env,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml

_SAFETY = Path(__file__).parents[3] / "configs/safety.live.yaml"


async def _database(monkeypatch, tmp_path, *, phase: str, realm: str | None, process_realm: str):
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    configure_account_env(monkeypatch)
    values = {"BFX_PHASE": phase, "BFX_DEPLOYMENT_ENV": process_realm, "BFX_HEALTHZ_PORT": "0",
              "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'realm.db'}"}
    if phase == "paper":
        values.update({"BFX_EXECUTION_POLICY": "paper", "BFX_ALLOCATION_CAP_USDT": "500"})
    if phase == "live":
        values.update({
            "BFX_EXECUTOR": "bitfinex_live", "BFX_WS_CLIENT_ENABLED": "true",
            "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
            "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
            "BFX_SAFETY_CONFIG": str(_SAFETY),
        })
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine, capital_policies=False)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM database_realm"))
        if realm is not None:
            await conn.execute(text(
                "INSERT INTO database_realm (realm, stamped_at_ms, actor) VALUES (:r, 0, 'test')"),
                {"r": realm})
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["live", "paper"])
@pytest.mark.parametrize(("stamp", "match"), [
    (None, "database_realm_unstamped"),
    ("prod", "database_realm_mismatch stamp=prod process=ci"),
    ("shadow", "database_realm_mismatch stamp=shadow process=ci"),
])
async def test_boot_refuses_an_unstamped_or_foreign_database(
    monkeypatch, tmp_path, httpx_mock, phase, stamp, match
) -> None:
    from bfx_funding_bot.apps.bot import build_daemon

    engine = await _database(monkeypatch, tmp_path, phase=phase, realm=stamp, process_realm="ci")
    try:
        with pytest.raises(DatabaseRealmMismatch, match=match):
            await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
        assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_boot_refuses_a_database_without_the_table(monkeypatch, tmp_path) -> None:
    from bfx_funding_bot.apps.bot import build_daemon

    engine = await _database(monkeypatch, tmp_path, phase="paper", realm="ci", process_realm="ci")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE database_realm"))
        with pytest.raises(DatabaseRealmMismatch, match="database_realm_missing"):
            await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_matching_stamp_passes_the_realm_check(monkeypatch, tmp_path) -> None:
    from bfx_funding_bot.core.database_realm import assert_database_realm, read_database_realm

    engine = await _database(monkeypatch, tmp_path, phase="paper", realm="ci", process_realm="ci")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            assert await read_database_realm(session) == "ci"
            assert await assert_database_realm(session, "ci") == "ci"
            with pytest.raises(DatabaseRealmMismatch, match="mismatch stamp=ci process=prod"):
                await assert_database_realm(session, "prod")
            with pytest.raises(DatabaseRealmMismatch, match="expected_unknown"):
                await assert_database_realm(session, "")
    finally:
        await engine.dispose()
