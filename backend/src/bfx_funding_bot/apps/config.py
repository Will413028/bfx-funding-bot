"""Application configuration loaders."""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from pathlib import Path

import yaml

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment
from bfx_funding_bot.modules.strategy import CellConfig

log = logging.getLogger(__name__)

# Capital snapshots older than this block authorization. The bot and the
# capital comparison command must use the same value.
CAPITAL_MAX_SNAPSHOT_AGE_MS = 300_000

# Phase 3b WFO qualification result (per-cell detail in the 2026-05-18 phase 3b
# WFO research report, kept outside this repository). Any edit here MUST be
# reconciled with that report.
UNQUALIFIED_PAIRS = {("mean_reversion", "fUST_p30")}
QUALIFIED_PAIRS = {
    ("rate_percentile", "fUSD_p2"), ("rate_percentile", "fUSD_p30"),
    ("rate_percentile", "fUSD_a30"), ("rate_percentile", "fUST_p2"),
    ("rate_percentile", "fUST_p30"), ("rate_percentile", "fUST_a30"),
    ("mean_reversion", "fUSD_p2"), ("mean_reversion", "fUSD_p30"),
    ("mean_reversion", "fUSD_a30"), ("mean_reversion", "fUST_p2"),
    ("mean_reversion", "fUST_a30"),
}


def load_config(*, cells_yaml_path: Path | None = None) -> MarketfeedConfig:
    phase_str = os.environ.get("BFX_PHASE", "").strip()
    if not phase_str:
        raise ValueError("BFX_PHASE env var required")
    if phase_str not in {"paper", "shadow", "live"}:
        # canary was retired with the per-build ceremony (ADR 2026-09-25).
        raise ValueError(f"BFX_PHASE must be paper, shadow, or live, got {phase_str!r}")
    if phase_str == "live":
        legacy = [name for name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
            "BFX_VENUE_FLOOR_USD", "BFX_MIN_OFFER_BUFFER_PCT",
        ) if name in os.environ]
        if legacy:
            raise ValueError(
                f"Remove legacy money env {legacy}; convert and validate applied CapitalPolicy first"
            )

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

    # Phase <-> realm fail-fast guard (defense in depth; the deploy env sets
    # the realm explicitly per phase, but reject obviously-wrong combos in case
    # an env is set by hand). phase = rollout/real-money dimension;
    # deployment_environment = data-isolation realm (prod/shadow/ci). live is
    # real money -> must land in prod (never shadow, which holds the Phase 4.3
    # calibration dataset). paper/shadow are simulated -> must never land in
    # prod (fake fills would corrupt real-money analytics). ci is the universal
    # test/dev realm and is always allowed.
    if phase_str == "live" and deployment_environment is DeploymentEnvironment.SHADOW:
        raise ValueError(
            f"BFX_PHASE={phase_str} (real money) must not run in the shadow realm "
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

    if phase_str == "live" and execution_policy is ExecutionPolicy.PAPER:
        raise ValueError(f"{phase_str} execution_policy must be live-capable, not paper")

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
            # (lives at backend/configs/, not src/bfx_funding_bot/configs/),
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


def load_cells_only(cells_yaml_path: Path) -> list[CellConfig]:
    """Parse just the `cells:` list from a YAML file, no env vars, no daemon config.

    For tooling (derive_cells, run_oos_profitability) and the deploy gate that
    need the deployed cell definitions without the full daemon environment.
    """
    if not cells_yaml_path.exists():
        raise FileNotFoundError(f"cells.yaml not found at {cells_yaml_path}")
    raw = yaml.safe_load(cells_yaml_path.read_text())
    return [CellConfig.model_validate(c) for c in raw.get("cells", [])]
