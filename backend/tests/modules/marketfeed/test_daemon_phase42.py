"""build_daemon: AccountContext + executor + chain + conditional fill_tracker."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.registry import ExecutorConfigError
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from tests.modules.marketfeed.account_test_helpers import (
    configure_account_env,
    seed_exchange_account,
)


def _write_cells_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


async def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mirror the env baseline used by existing test_daemon.py.

    Phase 4.4c: switched to file-based sqlite so event-store tables are
    visible to build_daemon's session_factory (from_snapshot at boot).
    """
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    db_path = tmp_path / "daemon_p42.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng)
    await _eng.dispose()


def _add_bitfinex_mock(httpx_mock: HTTPXMock) -> None:
    """warmup_cell fetches Bitfinex candles with file-based sqlite (tables exist)."""
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )


@pytest.mark.asyncio
async def test_build_daemon_wires_paper_executor_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    await _base_env(monkeypatch, tmp_path)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.apps.bot import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    assert isinstance(daemon.executor, EchoPaperExecutor)
    assert daemon.account_ctx.account_id == "550e8400-e29b-41d4-a716-446655440000"
    assert daemon.account_ctx.allocation_cap_usdt == Decimal("500")
    assert isinstance(daemon.safety_chain, SafetyGuardChain)
    assert daemon.fill_tracker is None


@pytest.mark.asyncio
async def test_build_daemon_simulated_excludes_buying_power_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """BuyingPowerGuard is a live-only backstop. It reads funding-wallet
    available balance, which is 0 until the first live reconcile — in the
    simulated path that would block every POST. The safety chain is inert in
    sim today only because its sole evaluator (DeploymentReconciler.deploy) is
    live-only; gate the guard explicitly so that contract is local, not an
    emergent invariant a future sim-path chain evaluation could violate.

    AllocationCapGuard (policy, balance-independent) stays in both paths."""
    await _base_env(monkeypatch, tmp_path)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)  # default paper -> simulated
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.apps.bot import build_daemon
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        AllocationCapGuard,
        BuyingPowerGuard,
    )
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    types = {type(g) for g in daemon.safety_chain.guards}
    assert BuyingPowerGuard not in types  # live-only — excluded in sim
    assert AllocationCapGuard in types    # policy guard — present in both paths


@pytest.mark.asyncio
async def test_build_daemon_invalid_executor_combo_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    await _base_env(monkeypatch, tmp_path)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_EXECUTOR", "paper")
    monkeypatch.setenv("BFX_FILL_TRACKER_ENABLED", "true")
    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.apps.bot import build_daemon
    with pytest.raises(ExecutorConfigError):
        await build_daemon(
            cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
        )


def _write_safety_yaml(tmp_path: Path, *, disable: set[str] | None = None) -> Path:
    """Write a safety.yaml with `disable` set marking which guards to flip off.

    Names: manual_kill / auth_health / heartbeat / allocation_cap.
    """
    disable = disable or set()

    def b(name: str, default: bool) -> str:
        return "false" if name in disable else ("true" if default else "false")

    path = tmp_path / "safety.yaml"
    path.write_text(f"""
hard_guards:
  manual_kill:
    enabled: {b("manual_kill", True)}
  auth_health:
    enabled: {b("auth_health", True)}
  heartbeat:
    enabled: {b("heartbeat", True)}
    sub_task_stale_threshold_seconds: 300
  allocation_cap:
    enabled: {b("allocation_cap", True)}
    caps: {{fUSD: 0, fUST: 3000}}
    default_cap: 0
  buying_power:
    enabled: true
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
""")
    return path


@pytest.mark.asyncio
async def test_build_daemon_filters_disabled_hard_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """M2 regression: SafetyConfig.<guard>.enabled flag was Pydantic-parsed
    but build_daemon ignored it — 7 guards always constructed regardless of
    yaml. Lock the contract: build_daemon must filter guards by enabled."""
    await _base_env(monkeypatch, tmp_path)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    # Disable heartbeat + allocation_cap from hard_guards.
    safety_yaml = _write_safety_yaml(
        tmp_path, disable={"heartbeat", "allocation_cap"},
    )
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))

    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.apps.bot import build_daemon
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        AllocationCapGuard,
        AuthHealthGuard,
        HeartbeatGuard,
        ManualKillGuard,
    )
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    guards = daemon.safety_chain.guards
    types = {type(g) for g in guards}

    # Enabled hard guards present.
    assert ManualKillGuard in types
    assert AuthHealthGuard in types
    # Disabled hard guards absent.
    assert HeartbeatGuard not in types
    assert AllocationCapGuard not in types
    # NAV drops only alert (lending envelope D3): no loss/drawdown guard exists.
    assert not {"realized_loss_24h", "drawdown_from_peak"} & {g.name for g in guards}


@pytest.mark.asyncio
async def test_build_daemon_heartbeat_guard_watches_market_data_not_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """HeartbeatGuard is a readiness gate ('don't trade on a stale market
    view'), so it watches market-data own-loop liveness (ws), NOT the reactive
    executor/safety_chain — watching those self-suppresses trading in quiet
    markets (the 2026-05-26 canary restart bug) and is a reactive mismatch."""
    await _base_env(monkeypatch, tmp_path)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    # heartbeat guard enabled (disable nothing).
    safety_yaml = _write_safety_yaml(tmp_path, disable=set())
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))
    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.apps.bot import build_daemon
    from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    hbg = next(
        g for g in daemon.safety_chain.guards if isinstance(g, HeartbeatGuard)
    )
    assert hbg.watched == ["ws"]
    assert "executor" not in hbg.watched
    assert "safety_chain" not in hbg.watched
