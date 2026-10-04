"""What a simulated boot refuses, and the one chain both venues compose (PostgreSQL).

Every refusal happens before the vault, any client or any worker exists: nothing reaches
a venue, nothing is written to the venue log. Guard-chain equality is the ADR's expected
outcome of one safety config for both venues (R5).
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from bfx_funding_bot.apps import bot
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.schema_head import SchemaHeadMismatch
from bfx_funding_bot.modules.ledger import PolicyRefused
from tests.modules.marketfeed.account_test_helpers import TEST_VAULT_KEK_B64

from .sim_daemon import close_sim_env, make_sim_env
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, exc: type[BaseException],  # noqa: F811
                   match: str, *, before: Any = None, **options: Any) -> None:
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path, **options)
    try:
        if before is not None:
            await before(engine)
        with pytest.raises(exc, match=match):
            await sim.build()
        # Nothing reached a venue, and the venue log stayed empty.
        assert [r for r in httpx_mock.get_requests() if r.method == "POST"] == []
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM sim_venue_event")) == 0
    finally:
        await close_sim_env(sim, engine)


async def test_a_simulated_boot_on_a_legacy_epoch_is_refused(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    """Mutation: the simulated venue's supported set includes ``legacy``."""
    await _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, AuthorityMismatch,
                   "authority_unsupported value=legacy build=ledger", epoch=None)


async def test_a_bitfinex_boot_on_a_real_ledger_epoch_is_refused(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    """Mutation: the Bitfinex venue's supported set includes ``ledger`` (no patch here)."""
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        monkeypatch.delenv("BFX_SIM_INITIAL_WALLETS")
        monkeypatch.setenv("BFX_PHASE", "live")
        monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
        monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)
        with pytest.raises(AuthorityMismatch, match="authority_unsupported value=ledger build=legacy"):
            await bot.build_daemon(cells_yaml_path=sim.cells_path, skip_ws=True)
    finally:
        await close_sim_env(sim, engine)


async def test_a_simulated_boot_refuses_a_vault_key_in_the_environment(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    await _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, ConfigurationError,
                   "BFX_VAULT_KEK must not be set",
                   env={"BFX_VAULT_KEK": TEST_VAULT_KEK_B64})


async def test_a_simulated_boot_without_a_ledger_policy_is_refused(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    await _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, PolicyRefused, "policy_unavailable", policy=False)


async def test_a_simulated_boot_on_a_database_not_at_schema_head_is_refused(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    async def stale(engine) -> None:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'a7f3c1d9e204'"))

    await _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, SchemaHeadMismatch,
                   "database=a7f3c1d9e204", before=stale)


async def test_a_simulated_boot_on_a_database_stamped_prod_is_refused(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    async def stamp_prod(engine) -> None:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE database_realm DISABLE TRIGGER USER"))
            await conn.execute(text("UPDATE database_realm SET realm = 'prod'"))
            await conn.execute(text("ALTER TABLE database_realm ENABLE TRIGGER USER"))

    await _refused(ledger_db, monkeypatch, httpx_mock, tmp_path, DatabaseRealmMismatch,
                   "database_realm_mismatch stamp=prod process=ci", before=stamp_prod)


async def test_both_venues_compose_the_same_guard_chain_and_workers(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    """Mutations: omit or reorder ``CapitalPolicyGuard`` / a pre-trade guard / the writer-lock
    guard for one venue; leave the reconciler out of one venue's composition."""
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        simulated = await sim.build()
        simulated_view = _composition(simulated)
        await simulated.writer_lock.release()  # same account and realm: one advisory lock
        simulated.writer_lock = None

        monkeypatch.delenv("BFX_SIM_INITIAL_WALLETS")
        monkeypatch.setenv("BFX_PHASE", "live")
        monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
        monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)

        async def ledger_epoch(_session: object, *, supported: object) -> str:
            return "ledger"  # as test_daemon_authority_wiring: production still refuses it

        monkeypatch.setattr(bot, "read_authority", ledger_epoch)
        bitfinex = await bot.build_daemon(cells_yaml_path=sim.cells_path, skip_ws=True)
        sim.daemons.append(bitfinex)
        bitfinex_view = _composition(bitfinex)

        assert simulated_view["guards"] == bitfinex_view["guards"]
        assert {"CapitalPolicyGuard", "WriterLockGuard"} <= set(simulated_view["guards"])
        assert simulated_view["guards"][-1] == "WriterLockGuard"
        # Same reconciler, workers and syncs; only the auth WS (Bitfinex's) differs.
        assert simulated_view["workers"] == bitfinex_view["workers"]
        assert simulated_view["workers"]["periodic_reconcile"] is True
        assert simulated_view["workers"]["interest_ledger_sync"] is True
        assert simulated_view["workers"]["credit_history_sync"] is True
        assert (simulated_view["auth_ws"], bitfinex_view["auth_ws"]) == (False, True)
        assert simulated_view["kill_venue"] == bitfinex_view["kill_venue"] == "BitfinexLiveExecutor"
    finally:
        await close_sim_env(sim, engine)


def _composition(daemon: Any) -> dict[str, Any]:
    names = (
        "periodic_reconcile", "boot_recovery", "uncertainty_worker", "trading_control",
        "capital_policy_control", "protection", "writer_lock", "writer_lock_watch",
        "command_gate", "interest_ledger_sync", "credit_history_sync", "funding_book_service",
        "trading_status", "signal_engine",
    )
    return {
        "guards": [type(g).__name__ for g in daemon.safety_chain.guards],
        "workers": {name: getattr(daemon, name) is not None for name in names},
        "auth_ws": daemon.auth_ws is not None,
        "kill_venue": type(daemon.trading_control.kill_switch._venue).__name__,
    }
