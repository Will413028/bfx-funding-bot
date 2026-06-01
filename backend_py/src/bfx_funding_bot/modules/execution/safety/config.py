"""SafetyConfig: Pydantic-validated yaml loader.

No hot reload — config is immutable for daemon lifetime. Config change =
redeploy. CalibratedGuard threshold values are nullable (4.2 disabled
defaults), but enabled=True requires non-null threshold (validator below).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ManualKillCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class _AuthHealthCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class _HeartbeatCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    sub_task_stale_threshold_seconds: Annotated[int, Field(gt=0)]


class _AllocationCapCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    caps: dict[str, Decimal] = {}
    default_cap: Decimal = Decimal("0")


class _BuyingPowerCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    buffers: dict[str, Decimal] = {}
    default_buffer: Decimal = Decimal("0")


class HardGuardsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manual_kill: _ManualKillCfg
    auth_health: _AuthHealthCfg
    heartbeat: _HeartbeatCfg
    allocation_cap: _AllocationCapCfg
    buying_power: _BuyingPowerCfg


class _RealizedLossCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    threshold_pct: Annotated[float, Field(gt=0)] | None

    @model_validator(mode="after")
    def _check(self) -> _RealizedLossCfg:
        if self.enabled and self.threshold_pct is None:
            raise ValueError("realized_loss_24h enabled=True requires threshold_pct")
        return self


class _DrawdownCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    threshold_pct: Annotated[float, Field(gt=0)] | None

    @model_validator(mode="after")
    def _check(self) -> _DrawdownCfg:
        if self.enabled and self.threshold_pct is None:
            raise ValueError("drawdown_from_peak enabled=True requires threshold_pct")
        return self


class _DivergenceCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    threshold_pct: Annotated[float, Field(gt=0)] | None
    window_minutes: Annotated[int, Field(gt=0)] | None

    @model_validator(mode="after")
    def _check(self) -> _DivergenceCfg:
        if self.enabled and (self.threshold_pct is None or self.window_minutes is None):
            raise ValueError("divergence_rate enabled=True requires threshold_pct + window_minutes")
        return self


class CalibratedGuardsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    realized_loss_24h: _RealizedLossCfg
    drawdown_from_peak: _DrawdownCfg
    divergence_rate: _DivergenceCfg


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hard_guards: HardGuardsCfg
    calibrated_guards: CalibratedGuardsCfg


def load_safety_config(path: Path) -> SafetyConfig:
    raw = yaml.safe_load(path.read_text())
    return SafetyConfig.model_validate(raw)
