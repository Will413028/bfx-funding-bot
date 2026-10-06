"""build_daemon: AccountContext + executor + chain + the auth WebSocket (Bitfinex venue)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    boot_live_construction,
)


@pytest.mark.asyncio
async def test_build_daemon_wires_the_bitfinex_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon, engine = await boot_live_construction(monkeypatch, tmp_path, httpx_mock)
    try:
        assert isinstance(daemon.executor, BitfinexLiveExecutor)
        assert daemon.account_ctx.account_id == str(TEST_EXCHANGE_ACCOUNT_ID)
        assert daemon.account_ctx.allocation_cap_usdt == Decimal("0")  # capital is the policy's
        assert isinstance(daemon.safety_chain, SafetyGuardChain)
        assert not hasattr(daemon, "fill_tracker")  # the REST fill tracker is gone
        assert daemon.writer_lock is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_env", ["", "paper", "bitfinex_live", "foo"])
async def test_a_set_bfx_executor_refuses_the_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
    executor_env: str,
) -> None:
    """The executor knob is gone: whatever it holds, a set value is a config error."""
    with pytest.raises(ValueError, match="BFX_EXECUTOR"):
        await boot_live_construction(
            monkeypatch, tmp_path, httpx_mock, extra_env={"BFX_EXECUTOR": executor_env},
        )


@pytest.mark.asyncio
async def test_the_live_guard_chain_is_capital_policy_not_legacy_caps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Capital limits are the applied policy's and the offer envelope's: the chain holds no
    allocation-cap or buying-power guard, and ends with the writer-lock guard."""
    daemon, engine = await boot_live_construction(monkeypatch, tmp_path, httpx_mock)
    try:
        names = [g.name for g in daemon.safety_chain.guards]
        assert names[:3] == ["manual_kill", "uncertainty", "auth_health"]
        assert "capital_policy" in names
        assert names[-1] == "writer_lock"
        assert not {"allocation_cap", "buying_power"} & set(names)
        assert [type(g).__name__ for g in daemon.safety_chain.guards].count("CapitalPolicyGuard") == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_bitfinex_venue_always_composes_the_auth_websocket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """CC4: REST submit without the fill WS is stale exposure, so the venue's capabilities
    compose it; no flag exists to turn it off (a retired one is refused by the config)."""
    daemon, engine = await boot_live_construction(monkeypatch, tmp_path, httpx_mock)
    try:
        assert daemon.auth_ws is not None and daemon.ws_dispatcher is not None
    finally:
        await engine.dispose()


def _write_safety_yaml(tmp_path: Path, *, disable: set[str] | None = None) -> Path:
    """Write a safety config with `disable` marking which guards to flip off.

    Names: manual_kill / auth_health / heartbeat.
    """
    disable = disable or set()

    def b(name: str) -> str:
        return "false" if name in disable else "true"

    path = tmp_path / "safety.yaml"
    path.write_text(f"""
hard_guards:
  manual_kill:
    enabled: {b("manual_kill")}
  auth_health:
    enabled: {b("auth_health")}
  heartbeat:
    enabled: {b("heartbeat")}
    sub_task_stale_threshold_seconds: 300
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
pre_trade_limits:
  command_rate: {{capacity: 12, refill_per_second: 0.1, trip_blocks: 6, trip_window_seconds: 300}}
""")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["manual_kill", "auth_health", "heartbeat"])
async def test_build_daemon_refuses_a_live_boot_with_a_required_guard_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock, guard: str,
) -> None:
    """M2/live: a flag that turns a required guard off is refused, never honoured."""
    safety_yaml = _write_safety_yaml(tmp_path, disable={guard})
    with pytest.raises(ValueError, match=f"disabled: \\['{guard}'\\]"):
        await boot_live_construction(
            monkeypatch, tmp_path, httpx_mock, extra_env={"BFX_SAFETY_CONFIG": str(safety_yaml)},
        )


@pytest.mark.asyncio
async def test_build_daemon_heartbeat_guard_watches_market_data_not_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """HeartbeatGuard is a readiness gate ('don't trade on a stale market
    view'), so it watches market-data own-loop liveness (ws), NOT the reactive
    executor/safety_chain — watching those self-suppresses trading in quiet
    markets (the 2026-05-26 canary restart bug) and is a reactive mismatch."""
    from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard

    safety_yaml = _write_safety_yaml(tmp_path)
    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, extra_env={"BFX_SAFETY_CONFIG": str(safety_yaml)},
    )
    try:
        hbg = next(g for g in daemon.safety_chain.guards if isinstance(g, HeartbeatGuard))
        assert hbg.watched == ["ws"]
        assert "executor" not in hbg.watched
        assert "safety_chain" not in hbg.watched
    finally:
        await engine.dispose()
