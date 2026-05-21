"""SafetyConfig yaml load + validation."""
from __future__ import annotations

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
    cfg = load_safety_config(yaml_path)
    assert cfg.hard_guards.manual_kill.enabled is True
    assert cfg.hard_guards.heartbeat.sub_task_stale_threshold_seconds == 300
    assert cfg.calibrated_guards.realized_loss_24h.enabled is False


def test_calibrated_enabled_requires_threshold(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap: {enabled: true}
calibrated_guards:
  realized_loss_24h:
    enabled: true
    threshold_usdt: null
  drawdown_from_peak: {enabled: false, threshold_pct: null}
  divergence_rate: {enabled: false, threshold_pct: null, window_minutes: null}
""")
    with pytest.raises(ValidationError, match="threshold_usdt"):
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
calibrated_guards:
  realized_loss_24h: {enabled: false, threshold_usdt: null}
  drawdown_from_peak: {enabled: false, threshold_pct: null}
  divergence_rate: {enabled: false, threshold_pct: null, window_minutes: null}
""")
    with pytest.raises(ValidationError, match="greater than 0"):
        load_safety_config(yaml_path)
