"""SafetyConfig yaml load + validation."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.execution.safety.config import load_safety_config


def test_default_yaml_loads(tmp_path: Path) -> None:
    yaml_path = tmp_path / "safety.yaml"
    yaml_path.write_text("""
hard_guards:
  manual_kill:
    enabled: true
  auth_health:
    enabled: true
  heartbeat:
    enabled: true
    sub_task_stale_threshold_seconds: 300
  allocation_cap:
    enabled: true
  buying_power:
    enabled: true
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
""")
    cfg = load_safety_config(yaml_path)
    assert cfg.hard_guards.manual_kill.enabled is True
    assert cfg.hard_guards.heartbeat.sub_task_stale_threshold_seconds == 300
    assert cfg.nav_alerts.realized_loss_24h_pct is None


def test_retired_calibrated_guards_no_longer_load(tmp_path: Path) -> None:
    """NAV drops only alert now (lending envelope D3); the old guard section is refused."""
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap: {enabled: true}
  buying_power: {enabled: true}
calibrated_guards:
  realized_loss_24h: {enabled: true, threshold_pct: 5}
""")
    with pytest.raises(ValidationError):
        load_safety_config(yaml_path)


def test_missing_yaml_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_safety_config(tmp_path / "nope.yaml")


def test_negative_heartbeat_threshold_rejected(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bad_heartbeat.yaml"
    yaml_path.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: -1}
  allocation_cap: {enabled: true}
  buying_power: {enabled: true}
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
""")
    with pytest.raises(ValidationError, match="greater than 0"):
        load_safety_config(yaml_path)


def test_live_nav_drop_alerts_at_5_and_10_pct() -> None:
    cfg = load_safety_config(Path(__file__).parents[4] / "configs" / "safety.live.yaml")
    # percentage of NAV (auto-scales with funded capital), not an absolute USDT amount
    assert (cfg.nav_alerts.realized_loss_24h_pct, cfg.nav_alerts.drawdown_pct) == (5.0, 10.0)


def test_caps_and_buffers_maps_parse(tmp_path: Path) -> None:
    p = tmp_path / "safety.yaml"
    p.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap:
    enabled: true
    caps: {fUST: 3000, fUSD: 0}
    default_cap: 0
  buying_power:
    enabled: true
    buffers: {fUST: 3, fUSD: 3}
    default_buffer: 0
nav_alerts:
  realized_loss_24h_pct: null
  drawdown_pct: null
""")
    cfg = load_safety_config(p)
    assert cfg.hard_guards.allocation_cap.caps["fUST"] == Decimal("3000")
    assert cfg.hard_guards.allocation_cap.caps["fUSD"] == Decimal("0")
    assert cfg.hard_guards.buying_power.buffers["fUSD"] == Decimal("3")
    assert cfg.hard_guards.allocation_cap.default_cap == Decimal("0")
    assert cfg.hard_guards.buying_power.default_buffer == Decimal("0")
