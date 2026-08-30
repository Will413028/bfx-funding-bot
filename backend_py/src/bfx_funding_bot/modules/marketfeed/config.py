"""Marketfeed daemon configuration loader.

Read env var + cells.yaml, run Pydantic validation, fail-fast on invalid input.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from pydantic import ValidationInfo

from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment

log = logging.getLogger(__name__)

# Phase 3b WFO qualification result. Source of truth:
# docs/research/2026-05-18-phase3b-wfo-results.md (Per-Cell Detail section).
# Any edit here MUST be reconciled with that doc.
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
        elif strat == StrategyName.ADAPTIVE_PERIOD:
            AdaptivePeriodParams.model_validate(v)
        return v

    @field_validator("staleness_budget_hours")
    @classmethod
    def _validate_budget(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("staleness_budget_hours must be positive")
        return v


class MarketfeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Annotated[Phase, Field(description="paper / shadow / canary")]
    cells: list[CellConfig]
    database_url: str
    deployment_environment: DeploymentEnvironment
    execution_policy: ExecutionPolicy
    book_max_age_seconds: float | None = Field(default=None, gt=0)
    book_reconcile_interval_seconds: float | None = Field(default=None, gt=0)
    book_max_down_pct: float | None = Field(default=None, ge=0, le=1)
    optimizer_fee_rate: Decimal | None = Field(default=None, ge=0, le=1)
    fill_model_artifact: str | None = None
    redis_url: str | None = None
    run_duration_hours: int | None = Field(default=None, gt=0)
    # Bug C fix (5/20): scheduler observe-after-close buffer. Was 5s
    # default — but Bitfinex p30 candles sometimes land in DB > 5s after
    # hh:00 → scheduler reads 0 rows → mis-emits health degraded (Bug A).
    # 30s is the new default; calibration period (Phase 4.3) tunes via
    # BFX_SCHEDULER_BUFFER_S env override.
    scheduler_buffer_s: float = Field(default=30.0, gt=0)
    # Phase 4.3 LOCF: global default staleness budget for LOCF fill.
    # p30 sparse cells override per-entry in cells.yaml (12h).
    # Override via BFX_STALENESS_BUDGET_HOURS_DEFAULT env var.
    staleness_budget_hours_default: int = Field(default=2, ge=1)


def load_config(*, cells_yaml_path: Path | None = None) -> MarketfeedConfig:
    phase_str = os.environ.get("BFX_PHASE", "").strip()
    if not phase_str:
        raise ValueError("BFX_PHASE env var required")
    if phase_str not in {"paper", "shadow", "canary"}:
        raise ValueError(f"BFX_PHASE must be paper, shadow, or canary, got {phase_str!r}")

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL required")
    redis_url = os.environ.get("REDIS_URL") or None

    deployment_env_str = os.environ.get("BFX_DEPLOYMENT_ENV", "").strip()
    if not deployment_env_str:
        raise ValueError("BFX_DEPLOYMENT_ENV required (one of: prod, shadow, ci)")
    try:
        deployment_environment = DeploymentEnvironment(deployment_env_str)
    except ValueError:
        valid = ", ".join(e.value for e in DeploymentEnvironment)
        raise ValueError(
            f"BFX_DEPLOYMENT_ENV must be one of {valid}, got {deployment_env_str!r}"
        ) from None

    # Phase <-> realm fail-fast guard (defense in depth; deploy-koyeb.sh sets
    # the realm explicitly per phase, but reject obviously-wrong combos in case
    # an env is set by hand). phase = rollout/real-money dimension;
    # deployment_environment = data-isolation realm (prod/shadow/ci). canary is
    # real money -> must land in prod (never shadow, which holds the Phase 4.3
    # calibration dataset). paper/shadow are simulated -> must never land in
    # prod (fake fills would corrupt real-money analytics). ci is the universal
    # test/dev realm and is always allowed.
    if phase_str == "canary" and deployment_environment is DeploymentEnvironment.SHADOW:
        raise ValueError(
            "BFX_PHASE=canary (real money) must not run in the shadow realm "
            "(BFX_DEPLOYMENT_ENV=shadow) -- it would pollute the simulated "
            "calibration dataset. Use prod (or ci for tests)."
        )
    if phase_str in {"paper", "shadow"} and deployment_environment is DeploymentEnvironment.PROD:
        raise ValueError(
            f"BFX_PHASE={phase_str} (simulated) must not run in the prod realm "
            "(BFX_DEPLOYMENT_ENV=prod) -- it would corrupt real-money analytics. "
            "Use shadow (or ci for tests)."
        )

    execution_policy_raw = os.environ.get("BFX_EXECUTION_POLICY", "").strip()
    if not execution_policy_raw:
        raise ValueError("BFX_EXECUTION_POLICY env var required")
    try:
        execution_policy = ExecutionPolicy(execution_policy_raw)
    except ValueError:
        valid = ", ".join(policy.value for policy in ExecutionPolicy)
        raise ValueError(
            f"BFX_EXECUTION_POLICY must be one of {valid}, got {execution_policy_raw!r}"
        ) from None

    if phase_str == "canary" and execution_policy is ExecutionPolicy.PAPER:
        raise ValueError("canary execution_policy must be live-capable, not paper")

    def required_float(name: str) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            raise ValueError(f"{name} required for {execution_policy.value} execution policy")
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}") from None

    live_capable_policies = {
        ExecutionPolicy.BOOK_GUARDED,
        ExecutionPolicy.OPTIMIZER_SHADOW,
        ExecutionPolicy.OPTIMIZER_LIVE,
    }
    book_max_age_seconds: float | None = None
    book_reconcile_interval_seconds: float | None = None
    book_max_down_pct: float | None = None
    if execution_policy in live_capable_policies:
        book_max_age_seconds = required_float("BFX_BOOK_MAX_AGE_SECONDS")
        book_reconcile_interval_seconds = required_float(
            "BFX_BOOK_RECONCILE_INTERVAL_SECONDS"
        )
        book_max_down_pct = required_float("BFX_BOOK_MAX_DOWN_PCT")

    fill_model_artifact = os.environ.get("BFX_FILL_MODEL_ARTIFACT", "").strip()
    if execution_policy is ExecutionPolicy.OPTIMIZER_LIVE and not fill_model_artifact:
        raise ValueError("BFX_FILL_MODEL_ARTIFACT required for optimizer_live execution policy")

    optimizer_fee_rate_raw = os.environ.get("BFX_OPTIMIZER_FEE_RATE", "").strip()
    if optimizer_fee_rate_raw:
        try:
            optimizer_fee_rate = Decimal(optimizer_fee_rate_raw)
        except Exception:
            raise ValueError(
                f"BFX_OPTIMIZER_FEE_RATE must be a decimal, got {optimizer_fee_rate_raw!r}",
            ) from None
        if not optimizer_fee_rate.is_finite() or not Decimal("0") <= optimizer_fee_rate <= Decimal("1"):
            raise ValueError("BFX_OPTIMIZER_FEE_RATE must be between 0 and 1")
    else:
        if execution_policy is ExecutionPolicy.OPTIMIZER_LIVE:
            raise ValueError(
                "BFX_OPTIMIZER_FEE_RATE required for optimizer_live execution policy",
            )
        optimizer_fee_rate = None

    run_duration = os.environ.get("BFX_RUN_DURATION_HOURS")
    run_duration_h: int | None
    if run_duration:
        try:
            run_duration_h = int(run_duration)
        except ValueError as e:
            raise ValueError(
                f"BFX_RUN_DURATION_HOURS must be an integer, got {run_duration!r}"
            ) from e
    else:
        run_duration_h = None

    scheduler_buffer_env = os.environ.get("BFX_SCHEDULER_BUFFER_S", "").strip()
    scheduler_buffer_s = 30.0
    if scheduler_buffer_env:
        try:
            scheduler_buffer_s = float(scheduler_buffer_env)
        except ValueError as e:
            raise ValueError(
                f"BFX_SCHEDULER_BUFFER_S must be a number, got {scheduler_buffer_env!r}"
            ) from e

    raw_staleness_budget_env = os.environ.get("BFX_STALENESS_BUDGET_HOURS_DEFAULT")
    staleness_budget_default: int | None = None
    if raw_staleness_budget_env is not None:
        try:
            staleness_budget_default = int(raw_staleness_budget_env)
        except ValueError as e:
            raise ValueError(
                f"BFX_STALENESS_BUDGET_HOURS_DEFAULT must be an integer, got "
                f"{raw_staleness_budget_env!r}"
            ) from e

    if cells_yaml_path is None:
        attempted: list[str] = []

        env_path = os.environ.get("BFX_CELLS_YAML", "").strip()
        if env_path:
            cells_yaml_path = Path(env_path)
            if cells_yaml_path.exists():
                attempted.append(f"BFX_CELLS_YAML env ({env_path}) [found]")
            else:
                attempted.append(f"BFX_CELLS_YAML env ({env_path}) [set but not found]")
        else:
            attempted.append("BFX_CELLS_YAML env (not set)")

        if cells_yaml_path is None or not cells_yaml_path.exists():
            cwd_path = Path.cwd() / "configs" / "cells.yaml"
            attempted.append(f"cwd: {cwd_path}")
            if cwd_path.exists():
                cells_yaml_path = cwd_path

        if cells_yaml_path is None or not cells_yaml_path.exists():
            attempted.append("importlib package: bfx_funding_bot/configs/cells.yaml")
            # Tier 3: importlib.resources (packaged resource)
            # NOTE: configs/ is currently outside the Python package tree
            # (lives at backend_py/configs/, not src/bfx_funding_bot/configs/),
            # so this tier resolves nothing in current packaging. Retained
            # per spec D1 intent — wire up when configs/ moves into the
            # package tree or pyproject.toml package-data is configured.
            try:
                import importlib.resources
                pkg_root = importlib.resources.files("bfx_funding_bot")
                pkg_path = pkg_root / "configs" / "cells.yaml"
                pkg_path_str = str(pkg_path)
                if Path(pkg_path_str).is_file():
                    cells_yaml_path = Path(pkg_path_str)
            except (ImportError, ModuleNotFoundError, FileNotFoundError, AttributeError, TypeError):
                pass

        if cells_yaml_path is None or not cells_yaml_path.exists():
            raise FileNotFoundError(
                "cells.yaml not found. Tried: " + " ; ".join(attempted)
            )
    elif not cells_yaml_path.exists():
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

    config_kwargs: dict[str, object] = {
        "phase": Phase(phase_str),
        "cells": cells,
        "database_url": database_url,
        "deployment_environment": deployment_environment,
        "execution_policy": execution_policy,
        "book_max_age_seconds": book_max_age_seconds,
        "book_reconcile_interval_seconds": book_reconcile_interval_seconds,
        "book_max_down_pct": book_max_down_pct,
        "optimizer_fee_rate": optimizer_fee_rate,
        "fill_model_artifact": fill_model_artifact or None,
        "redis_url": redis_url,
        "run_duration_hours": run_duration_h,
        "scheduler_buffer_s": scheduler_buffer_s,
    }
    if staleness_budget_default is not None:
        config_kwargs["staleness_budget_hours_default"] = staleness_budget_default
    config = MarketfeedConfig(**config_kwargs)
    for cell in config.cells:
        if cell.staleness_budget_hours is None:
            cell.staleness_budget_hours = config.staleness_budget_hours_default
    return config


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


def load_cells_only(cells_yaml_path: Path) -> list[CellConfig]:
    """Parse just the `cells:` list from a YAML file, no env vars, no daemon config.

    For tooling (derive_cells, run_oos_profitability) and the deploy gate that
    need the deployed cell definitions without the full daemon environment.
    """
    if not cells_yaml_path.exists():
        raise FileNotFoundError(f"cells.yaml not found at {cells_yaml_path}")
    raw = yaml.safe_load(cells_yaml_path.read_text())
    return [CellConfig.model_validate(c) for c in raw.get("cells", [])]
