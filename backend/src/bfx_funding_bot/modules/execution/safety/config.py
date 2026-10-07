"""SafetyConfig: Pydantic-validated yaml loader.

No hot reload — config is immutable for daemon lifetime. Config change =
redeploy. CalibratedGuard threshold values are nullable (4.2 disabled
defaults), but enabled=True requires non-null threshold (validator below).
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _ManualKillCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class _AuthHealthCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class _HeartbeatCfg(BaseModel):
    """Only the switch: the staleness threshold is the dependency-freshness one in
    core.health (DEPENDENCY_THRESHOLDS), shared with /readyz."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class HardGuardsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manual_kill: _ManualKillCfg
    auth_health: _AuthHealthCfg
    heartbeat: _HeartbeatCfg


class NavAlertsCfg(BaseModel):
    """NAV-drop alert thresholds (lending envelope D3 level 4); null disables one.

    Alerts only: funding NAV is native units, so lending cannot lower it --
    withdrawals, transfers or platform losses do, and stopping does not undo them.
    """
    model_config = ConfigDict(extra="forbid")
    realized_loss_24h_pct: Annotated[float, Field(gt=0)] | None
    drawdown_pct: Annotated[float, Field(gt=0)] | None


class _CommandRateCfg(BaseModel):
    """Token bucket over submits and cancels at the command gate."""
    model_config = ConfigDict(extra="forbid")
    capacity: Annotated[int, Field(gt=0)]
    refill_per_second: Annotated[float, Field(gt=0)]
    # This many throttled commands within the window trip HALTED/auto.
    trip_blocks: Annotated[int, Field(gt=0)]
    trip_window_seconds: Annotated[int, Field(gt=0)]


class PreTradeLimitsCfg(BaseModel):
    """Platform limits; per-symbol offer terms live in the CapitalPolicy envelope."""
    model_config = ConfigDict(extra="forbid")
    command_rate: _CommandRateCfg


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hard_guards: HardGuardsCfg
    nav_alerts: NavAlertsCfg
    # Optional in the schema; the daemon refuses to boot without it
    # (pre_trade.require_pre_trade_limits).
    pre_trade_limits: PreTradeLimitsCfg | None = None


def load_safety_config(path: Path) -> SafetyConfig:
    raw = yaml.safe_load(path.read_text())
    return SafetyConfig.model_validate(raw)
