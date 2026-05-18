"""Marketfeed daemon configuration loader.

Read env var + cells.yaml, run Pydantic validation, fail-fast on invalid input.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from pydantic import ValidationInfo

from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName

log = logging.getLogger(__name__)

UNQUALIFIED_PAIRS = {("mean_reversion", "fUST_p30")}
QUALIFIED_PAIRS = {
    ("rate_percentile", "fUSD_p2"), ("rate_percentile", "fUSD_p30"),
    ("rate_percentile", "fUSD_a30"), ("rate_percentile", "fUST_p2"),
    ("rate_percentile", "fUST_p30"), ("rate_percentile", "fUST_a30"),
    ("mean_reversion", "fUSD_p2"), ("mean_reversion", "fUSD_p30"),
    ("mean_reversion", "fUSD_a30"), ("mean_reversion", "fUST_p2"),
    ("mean_reversion", "fUST_a30"),
}


class MeanReversionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold_sigma: float = Field(gt=0)
    ratio_sigma: float = Field(gt=0)
    ema_alpha: float = Field(gt=0, lt=1)


class RatePercentileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percentile: int = Field(ge=1, le=99)
    lookback_hours: int = Field(ge=2)


class CellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: StrategyName
    symbol: str = Field(min_length=2)
    period_agg: str = Field(min_length=1)
    timeframe: Literal["15m", "30m", "1h"] = "1h"
    params: dict[str, Any]
    reference_amount_usdt: float = Field(gt=0, default=150.0)

    @property
    def cell_id(self) -> str:
        return f"{self.symbol}_{self.period_agg}"

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
        return v


class MarketfeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Annotated[Phase, Field(description="paper / shadow only -- canary rejected by daemon")]
    cells: list[CellConfig]
    axiom_api_key: str
    axiom_dataset: str
    database_url: str
    redis_url: str | None = None
    run_duration_hours: int | None = None


def load_config(*, cells_yaml_path: Path | None = None) -> MarketfeedConfig:
    phase_str = os.environ.get("BFX_PHASE", "").strip()
    if not phase_str:
        raise ValueError("BFX_PHASE env var required")
    if phase_str == "canary":
        raise ValueError("BFX_PHASE=canary not allowed in 4.1 daemon -- canary is 4.4 sub-spec")
    if phase_str not in {"paper", "shadow"}:
        raise ValueError(f"BFX_PHASE must be paper or shadow, got {phase_str!r}")

    axiom_api_key = os.environ.get("AXIOM_API_KEY", "")
    if not axiom_api_key:
        raise ValueError("AXIOM_API_KEY required")
    axiom_dataset = os.environ.get("AXIOM_DATASET", "")
    if not axiom_dataset:
        raise ValueError("AXIOM_DATASET required")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL required")
    redis_url = os.environ.get("REDIS_URL") or None
    run_duration = os.environ.get("BFX_RUN_DURATION_HOURS")
    run_duration_h = int(run_duration) if run_duration else None

    if cells_yaml_path is None:
        cells_yaml_path = Path(__file__).parents[4] / "configs" / "cells.yaml"
    if not cells_yaml_path.exists():
        raise FileNotFoundError(f"cells.yaml not found at {cells_yaml_path}")

    raw = yaml.safe_load(cells_yaml_path.read_text())
    cells = [CellConfig.model_validate(c) for c in raw.get("cells", [])]

    bfx_cells_env = os.environ.get("BFX_CELLS", "").strip()
    if bfx_cells_env:
        wanted = {item.strip() for item in bfx_cells_env.split(",") if item.strip()}
        yaml_ids = {c.pair_id for c in cells}
        for w in wanted:
            if w not in yaml_ids:
                raise ValueError(
                    f"BFX_CELLS entry {w!r} not in cells.yaml (have: {sorted(yaml_ids)})"
                )
        cells = [c for c in cells if c.pair_id in wanted]

    for c in cells:
        key = (c.strategy.value, c.cell_id)
        if key in UNQUALIFIED_PAIRS:
            log.warning(
                "cells.yaml contains unqualified Phase 3b pair %s -- running anyway",
                c.pair_id,
            )
    if not any((c.strategy.value, c.cell_id) in QUALIFIED_PAIRS for c in cells):
        log.warning("cells.yaml has no Phase 3b qualified pair -- all entries are exploratory")

    return MarketfeedConfig(
        phase=Phase(phase_str),
        cells=cells,
        axiom_api_key=axiom_api_key,
        axiom_dataset=axiom_dataset,
        database_url=database_url,
        redis_url=redis_url,
        run_duration_hours=run_duration_h,
    )
