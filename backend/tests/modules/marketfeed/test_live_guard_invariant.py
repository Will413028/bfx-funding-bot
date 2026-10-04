"""Live requires the operational guard set."""
from __future__ import annotations

from pathlib import Path

import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.safety.config import load_safety_config
from bfx_funding_bot.modules.marketfeed.daemon import assert_live_guard_invariant


def _safety_yaml(*, disable: str | None = None) -> str:
    on = dict.fromkeys(
        ("manual_kill", "auth_health", "heartbeat"),
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


def test_shadow_allows_disabled_guard(tmp_path: Path) -> None:
    assert_live_guard_invariant(Phase.SHADOW, _load(tmp_path, disable="manual_kill"))


@pytest.mark.parametrize("guard", ["manual_kill", "auth_health", "heartbeat"])
def test_live_keeps_required_noncapital_guards(tmp_path, guard):
    with pytest.raises(ValueError, match=guard):
        assert_live_guard_invariant(Phase.LIVE, _load(tmp_path, disable=guard))
