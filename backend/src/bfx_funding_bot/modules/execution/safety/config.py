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


class _SymbolLimitsCfg(BaseModel):
    """Always-on per-symbol pre-trade limits (T9). Every field is required."""
    model_config = ConfigDict(extra="forbid")
    # Bitfinex funding periods run 2..120 days; the guard enforces this range too.
    min_period_days: Annotated[int, Field(ge=2, le=120)]
    max_period_days: Annotated[int, Field(ge=2, le=120)]
    max_open_offers: Annotated[int, Field(gt=0)]
    # Offer rate must be at least this fraction of the median live bid rate.
    rate_floor_ratio: Annotated[Decimal, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def _check(self) -> _SymbolLimitsCfg:
        if self.min_period_days > self.max_period_days:
            raise ValueError("min_period_days must not exceed max_period_days")
        return self


class _CommandRateCfg(BaseModel):
    """Token bucket over submits and cancels at the command gate."""
    model_config = ConfigDict(extra="forbid")
    capacity: Annotated[int, Field(gt=0)]
    refill_per_second: Annotated[float, Field(gt=0)]
    # This many throttled commands within the window trip HALTED/auto.
    trip_blocks: Annotated[int, Field(gt=0)]
    trip_window_seconds: Annotated[int, Field(gt=0)]


class PreTradeLimitsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: dict[str, _SymbolLimitsCfg]
    command_rate: _CommandRateCfg


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hard_guards: HardGuardsCfg
    calibrated_guards: CalibratedGuardsCfg
    # Optional here so paper/simulation configs stay valid; the live daemon
    # refuses to boot without it (pre_trade.require_pre_trade_limits).
    pre_trade_limits: PreTradeLimitsCfg | None = None


def load_safety_config(path: Path) -> SafetyConfig:
    raw = yaml.safe_load(path.read_text())
    return SafetyConfig.model_validate(raw)
