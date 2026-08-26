"""Canary phase requires the full operational + loss-limit guard set enabled."""
from __future__ import annotations

from pathlib import Path

import pytest

from bfx_funding_bot.modules.execution.safety.config import _AllocationCapCfg, load_safety_config
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.daemon import (
    assert_canary_guard_invariant,
    assert_caps_invariant,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase


def _safety_yaml(*, disable: str | None = None) -> str:
    on = dict.fromkeys(
        ("manual_kill", "auth_health", "heartbeat",
         "allocation_cap", "realized_loss_24h", "drawdown_from_peak"),
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
calibrated_guards:
  realized_loss_24h:
    enabled: {b("realized_loss_24h")}
    threshold_pct: {15 if on["realized_loss_24h"] else "null"}
  drawdown_from_peak:
    enabled: {b("drawdown_from_peak")}
    threshold_pct: {15 if on["drawdown_from_peak"] else "null"}
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
"""


def _load(tmp_path: Path, disable: str | None = None):
    p = tmp_path / "safety.yaml"
    p.write_text(_safety_yaml(disable=disable))
    return load_safety_config(p)


def test_canary_ok_when_all_required_enabled(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert_canary_guard_invariant(Phase.CANARY, cfg)  # must not raise


@pytest.mark.parametrize("guard", [
    "manual_kill", "auth_health", "heartbeat",
    "allocation_cap", "realized_loss_24h", "drawdown_from_peak",
])
def test_canary_raises_when_required_guard_disabled(tmp_path: Path, guard: str) -> None:
    cfg = _load(tmp_path, disable=guard)
    with pytest.raises(ValueError, match=guard):
        assert_canary_guard_invariant(Phase.CANARY, cfg)


def test_shadow_allows_disabled_guard(tmp_path: Path) -> None:
    cfg = _load(tmp_path, disable="allocation_cap")
    assert_canary_guard_invariant(Phase.SHADOW, cfg)  # invariant is canary-only


_MR_PARAMS = {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_span": 100}


def _cell(symbol: str) -> CellConfig:
    return CellConfig(
        symbol=symbol, period_agg="a30", strategy="mean_reversion", params=_MR_PARAMS,
    )


def _alloc_cfg(caps: dict[str, int]) -> _AllocationCapCfg:
    return _AllocationCapCfg(enabled=True, caps=caps, default_cap=0)


def test_caps_invariant_raises_when_configured_symbol_missing() -> None:
    cells = [_cell("fUST")]
    with pytest.raises(ValueError, match="fUST"):
        assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUSD": 0}))


def test_caps_invariant_raises_when_canary_cap_zero() -> None:
    cells = [_cell("fUST")]
    with pytest.raises(ValueError, match=r"cap.*0|> 0"):
        assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUST": 0}))


def test_caps_invariant_ok_for_funded_canary() -> None:
    cells = [_cell("fUST")]
    assert assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUST": 3000})) is None


def test_caps_invariant_shadow_allows_zero_cap() -> None:
    # The cap>0 rejection is CANARY-only; under shadow a configured symbol may sit
    # at cap 0 (dark) as long as it has an explicit entry. Locks the phase asymmetry.
    cells = [_cell("fUST")]
    assert assert_caps_invariant(Phase.SHADOW, cells, _alloc_cfg({"fUST": 0})) is None


def test_caps_invariant_missing_entry_raises_in_all_phases() -> None:
    # The explicit-entry requirement is phase-independent (not gated on canary).
    cells = [_cell("fUST")]
    with pytest.raises(ValueError, match="fUST"):
        assert_caps_invariant(Phase.SHADOW, cells, _alloc_cfg({"fUSD": 0}))
