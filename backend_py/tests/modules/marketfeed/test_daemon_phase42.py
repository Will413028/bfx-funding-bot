"""build_daemon: AccountContext + executor + chain + conditional fill_tracker."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.registry import ExecutorConfigError
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain


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


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the env baseline used by existing test_daemon.py."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")


@pytest.mark.asyncio
async def test_build_daemon_wires_paper_executor_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    # Phase 4.4b D1: AxiomReplayQueryAdapter replay APL endpoint mocked.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/_apl?format=tabular",
        method="POST", status_code=200, json={"tables": []},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    assert isinstance(daemon.executor, EchoPaperExecutor)
    assert daemon.account_ctx.account_id == "default"
    assert daemon.account_ctx.allocation_cap_usdt == Decimal("500")
    assert isinstance(daemon.safety_chain, SafetyGuardChain)
    assert daemon.fill_tracker is None


@pytest.mark.asyncio
async def test_build_daemon_invalid_executor_combo_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_EXECUTOR", "paper")
    monkeypatch.setenv("BFX_FILL_TRACKER_ENABLED", "true")
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    # Phase 4.4b D1: AxiomReplayQueryAdapter replay APL endpoint mocked.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/_apl?format=tabular",
        method="POST", status_code=200, json={"tables": []},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    with pytest.raises(ExecutorConfigError):
        await build_daemon(
            cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
        )


def _write_safety_yaml(tmp_path: Path, *, disable: set[str] | None = None) -> Path:
    """Write a safety.yaml with `disable` set marking which guards to flip off.

    Names: manual_kill / auth_health / heartbeat / allocation_cap /
    realized_loss_24h / drawdown_from_peak / divergence_rate.
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
calibrated_guards:
  realized_loss_24h:
    enabled: false
    threshold_usdt: null
  drawdown_from_peak:
    enabled: false
    threshold_pct: null
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
""")
    return path


@pytest.mark.asyncio
async def test_build_daemon_filters_disabled_hard_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """M2 regression: SafetyConfig.<guard>.enabled flag was Pydantic-parsed
    but build_daemon ignored it — 7 guards always constructed regardless of
    yaml. Lock the contract: build_daemon must filter guards by enabled."""
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
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

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    # Phase 4.4b D1: AxiomReplayQueryAdapter replay APL endpoint mocked.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/_apl?format=tabular",
        method="POST", status_code=200, json={"tables": []},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
        DivergenceRateGuard,
        DrawdownGuard,
        RealizedLossGuard,
    )
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        AllocationCapGuard,
        AuthHealthGuard,
        HeartbeatGuard,
        ManualKillGuard,
    )
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
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
    # All calibrated guards disabled in this fixture → absent.
    assert RealizedLossGuard not in types
    assert DrawdownGuard not in types
    assert DivergenceRateGuard not in types


@pytest.mark.asyncio
async def test_build_daemon_includes_enabled_calibrated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """M2: an enabled calibrated guard (with a valid threshold) appears in
    the chain. Pairs with the disabled-default safety.yaml fixture."""
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    safety_yaml = tmp_path / "safety.yaml"
    safety_yaml.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap: {enabled: true}
calibrated_guards:
  realized_loss_24h:
    enabled: true
    threshold_usdt: 100.0
  drawdown_from_peak:
    enabled: false
    threshold_pct: null
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
""")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    # Phase 4.4b D1: AxiomReplayQueryAdapter replay APL endpoint mocked.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/_apl?format=tabular",
        method="POST", status_code=200, json={"tables": []},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
        DrawdownGuard,
        RealizedLossGuard,
    )
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    types = {type(g) for g in daemon.safety_chain.guards}
    assert RealizedLossGuard in types  # enabled
    assert DrawdownGuard not in types  # disabled


