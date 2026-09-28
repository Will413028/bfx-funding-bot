"""Pure strategy cell configuration contracts."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import StrategyName

if TYPE_CHECKING:
    from pydantic import ValidationInfo


class MeanReversionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold_sigma: float = Field(gt=0)
    ratio_sigma: float = Field(gt=0)
    ema_span: int = Field(ge=1)


class RatePercentileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percentile: int = Field(ge=1, le=99)
    lookback_hours: int = Field(ge=2)


class AdaptivePeriodParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ema_span: int = Field(ge=1)
    ratio_sigma: float = Field(gt=0)
    t1: float = Field(ge=0)
    t2: float = Field(gt=0)
    p_mid: int = Field(ge=2, le=120)
    p_long: int = Field(ge=2, le=120)

    @model_validator(mode="after")
    def _check_ordering(self) -> AdaptivePeriodParams:
        if not self.t2 > self.t1:
            raise ValueError("t2 must be > t1")
        if not self.p_long >= self.p_mid:
            raise ValueError("p_long must be >= p_mid")
        return self


def canonical_cell_id(symbol: str, period_agg: str) -> str:
    return f"{symbol}_{period_agg}"


class CellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: StrategyName
    symbol: str = Field(min_length=2)
    period_agg: str = Field(min_length=1)
    timeframe: Literal["15m", "30m", "1h"] = "1h"
    params: dict[str, Any]
    reference_amount_usdt: float = Field(gt=0, default=150.0)
    staleness_budget_hours: int | None = None  # None = use global default

    @property
    def cell_id(self) -> str:
        return canonical_cell_id(self.symbol, self.period_agg)

    @property
    def pair_id(self) -> str:
        return f"{self.strategy.value}:{self.cell_id}"

    @field_validator("params")
    @classmethod
    def _validate_params(cls, v: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        strat = info.data.get("strategy") if info.data else None
        if strat == StrategyName.MEAN_REVERSION:
            MeanReversionParams.model_validate(v)
        elif strat == StrategyName.RATE_PERCENTILE:
            RatePercentileParams.model_validate(v)
        elif strat == StrategyName.ADAPTIVE_PERIOD:
            AdaptivePeriodParams.model_validate(v)
        return v

    @field_validator("staleness_budget_hours")
    @classmethod
    def _validate_budget(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("staleness_budget_hours must be positive")
        return v


def configured_symbols(cells: list[CellConfig]) -> list[str]:
    """Distinct cell symbols, order-preserving — the per-currency reconcile loop's
    symbol set. Single-currency cells.yaml → a 1-element list (parity with the
    historic single-symbol BootRecovery)."""
    seen: set[str] = set()
    out: list[str] = []
    for c in cells:
        if c.symbol not in seen:
            seen.add(c.symbol)
            out.append(c.symbol)
    return out
