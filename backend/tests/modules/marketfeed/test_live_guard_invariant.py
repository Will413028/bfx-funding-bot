"""Live requires the operational guard set; simulation needs explicit caps."""
from __future__ import annotations

from pathlib import Path

import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.safety.config import _AllocationCapCfg, load_safety_config
from bfx_funding_bot.modules.marketfeed.daemon import (
    assert_caps_invariant,
    assert_live_guard_invariant,
)
from bfx_funding_bot.modules.strategy import CellConfig


def _safety_yaml(*, disable: str | None = None) -> str:
    on = dict.fromkeys(
        ("manual_kill", "auth_health", "heartbeat",
         "allocation_cap"),
        True,
    )
    if disable is not None:
        on[disable] = False

    def b(name: str) -> str:
        return "true" if on[name] else "false"

    return f"""
hard_guards:
  manual_kill:
    enabled: {b("manual_kill")}
  auth_health:
    enabled: {b("auth_health")}
  heartbeat:
    enabled: {b("heartbeat")}
    sub_task_stale_threshold_seconds: 300
  allocation_cap:
    enabled: {b("allocation_cap")}
  buying_power:
    enabled: true
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
"""


def _load(tmp_path: Path, disable: str | None = None):
    p = tmp_path / "safety.yaml"
    p.write_text(_safety_yaml(disable=disable))
    return load_safety_config(p)


def test_live_ok_when_all_required_enabled(tmp_path: Path) -> None:
    assert_live_guard_invariant(Phase.LIVE, _load(tmp_path))  # must not raise


def test_live_needs_no_allocation_cap_guard(tmp_path: Path) -> None:
    """Live sizes from the applied CapitalPolicy, not the allocation-cap flag."""
    assert_live_guard_invariant(Phase.LIVE, _load(tmp_path, disable="allocation_cap"))


def test_shadow_allows_disabled_guard(tmp_path: Path) -> None:
    assert_live_guard_invariant(Phase.SHADOW, _load(tmp_path, disable="manual_kill"))


@pytest.mark.parametrize("guard", ["manual_kill", "auth_health", "heartbeat"])
def test_live_keeps_required_noncapital_guards(tmp_path, guard):
    with pytest.raises(ValueError, match=guard):
        assert_live_guard_invariant(Phase.LIVE, _load(tmp_path, disable=guard))


_MR_PARAMS = {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_span": 100}


def _cell(symbol: str) -> CellConfig:
    return CellConfig(
        symbol=symbol, period_agg="a30", strategy="mean_reversion", params=_MR_PARAMS,
    )


def _alloc_cfg(caps: dict[str, int]) -> _AllocationCapCfg:
    return _AllocationCapCfg(enabled=True, caps=caps, default_cap=0)


def test_caps_invariant_raises_when_configured_symbol_missing() -> None:
    with pytest.raises(ValueError, match="fUST"):
        assert_caps_invariant([_cell("fUST")], _alloc_cfg({"fUSD": 0}))


def test_caps_invariant_allows_a_dark_zero_cap() -> None:
    # A configured symbol may sit at cap 0 (dark) as long as it has an explicit entry.
    assert assert_caps_invariant([_cell("fUST")], _alloc_cfg({"fUST": 0})) is None
