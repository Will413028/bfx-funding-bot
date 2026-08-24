from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols, load_config
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment


def _write_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "cells.yaml"
    p.write_text(yaml.safe_dump(content))
    return p


def _valid_yaml() -> dict:
    return {
        "cells": [
            {
                "strategy": "mean_reversion",
                "symbol": "fUSD",
                "period_agg": "a30",
                "timeframe": "1h",
                "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_span": 100},
                "reference_amount_usdt": 150.0,
            },
            {
                "strategy": "rate_percentile",
                "symbol": "fUSD",
                "period_agg": "a30",
                "timeframe": "1h",
                "params": {"percentile": 75, "lookback_hours": 168},
                "reference_amount_usdt": 150.0,
            },
        ],
        "phase3b_wfo_results_ref": "docs/research/2026-05-18-phase3b-wfo-results.md",
    }


def _set_required_config_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: str,
    policy: str,
    deployment_environment: str = "ci",
) -> None:
    monkeypatch.setenv("BFX_PHASE", phase)
    monkeypatch.setenv("BFX_EXECUTION_POLICY", policy)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", deployment_environment)
    if policy in {
        ExecutionPolicy.BOOK_GUARDED.value,
        ExecutionPolicy.OPTIMIZER_SHADOW.value,
        ExecutionPolicy.OPTIMIZER_LIVE.value,
    }:
        monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
        monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
        monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    if policy == ExecutionPolicy.OPTIMIZER_LIVE.value:
        monkeypatch.setenv("BFX_FILL_MODEL_ARTIFACT", "models/fUSD.json")


@pytest.fixture(autouse=True)
def _set_explicit_paper_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every config-load test declares an execution policy by default."""
    monkeypatch.setenv("BFX_EXECUTION_POLICY", ExecutionPolicy.PAPER.value)


def test_missing_execution_policy_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_EXECUTION_POLICY", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_EXECUTION_POLICY"):
        load_config(cells_yaml_path=yaml_path)


def test_canary_rejects_paper_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_config_env(monkeypatch, phase="canary", policy="paper")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=r"canary.*execution_policy"):
        load_config(cells_yaml_path=yaml_path)


def test_book_guarded_requires_all_book_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_config_env(monkeypatch, phase="shadow", policy="book_guarded")
    monkeypatch.delenv("BFX_BOOK_MAX_DOWN_PCT")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_BOOK_MAX_DOWN_PCT"):
        load_config(cells_yaml_path=yaml_path)


def test_optimizer_live_requires_fill_model_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_config_env(monkeypatch, phase="shadow", policy="optimizer_live")
    monkeypatch.delenv("BFX_FILL_MODEL_ARTIFACT")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_FILL_MODEL_ARTIFACT"):
        load_config(cells_yaml_path=yaml_path)


def test_optimizer_live_requires_optimizer_fee_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_config_env(monkeypatch, phase="shadow", policy="optimizer_live")
    monkeypatch.delenv("BFX_OPTIMIZER_FEE_RATE", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_OPTIMIZER_FEE_RATE"):
        load_config(cells_yaml_path=yaml_path)


def test_optimizer_live_exposes_artifact_and_decimal_fee_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_config_env(monkeypatch, phase="shadow", policy="optimizer_live")
    monkeypatch.setenv("BFX_FILL_MODEL_ARTIFACT", "models/fUST-fill.json")
    monkeypatch.setenv("BFX_OPTIMIZER_FEE_RATE", "0.15")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    config = load_config(cells_yaml_path=yaml_path)

    assert config.fill_model_artifact == "models/fUST-fill.json"
    assert config.optimizer_fee_rate == Decimal("0.15")


def test_optimizer_shadow_allows_missing_model_and_fee_for_observation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_config_env(monkeypatch, phase="shadow", policy="optimizer_shadow")
    monkeypatch.delenv("BFX_FILL_MODEL_ARTIFACT", raising=False)
    monkeypatch.delenv("BFX_OPTIMIZER_FEE_RATE", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    config = load_config(cells_yaml_path=yaml_path)

    assert config.fill_model_artifact is None
    assert config.optimizer_fee_rate is None


@pytest.mark.parametrize(
    ("legacy_name", "value"),
    [
        ("BFX_CLAMP_ENABLED", "false"),
        ("BFX_CLAMP_MAX_DOWN_PCT", "0.15"),
        ("BFX_CLAMP_TAKER_MAX_PERIOD_D", "7"),
    ],
)
def test_legacy_clamp_configuration_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    legacy_name: str, value: str,
) -> None:
    _set_required_config_env(monkeypatch, phase="paper", policy="paper")
    monkeypatch.setenv(legacy_name, value)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=legacy_name):
        load_config(cells_yaml_path=yaml_path)


def test_load_config_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.phase == "paper"
    assert len(cfg.cells) == 2
    assert cfg.cells[0].cell_id == "fUSD_a30"


def test_load_config_accepts_canary_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    _set_required_config_env(monkeypatch, phase="canary", policy="book_guarded")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.phase == "canary"


def test_canary_phase_rejects_shadow_realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real-money canary must never write to the shadow (simulated) realm —
    it would pollute the Phase 4.3 calibration dataset. Regression for the
    residual-env config drift (canary deploy inherited BFX_DEPLOYMENT_ENV=shadow)."""
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "shadow")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=r"canary.*must not.*shadow"):
        load_config(cells_yaml_path=yaml_path)


def test_canary_phase_accepts_prod_realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    _set_required_config_env(
        monkeypatch,
        phase="canary",
        policy="book_guarded",
        deployment_environment="prod",
    )
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.phase == "canary"
    assert cfg.deployment_environment == DeploymentEnvironment.PROD


@pytest.mark.parametrize("sim_phase", ["paper", "shadow"])
def test_simulated_phase_rejects_prod_realm(
    sim_phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Simulated phases must never write to the prod (real-money) realm —
    fake fills would corrupt prod analytics."""
    monkeypatch.setenv("BFX_PHASE", sim_phase)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=r"simulated.*must not.*prod"):
        load_config(cells_yaml_path=yaml_path)


def test_shadow_phase_accepts_shadow_realm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "shadow")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.deployment_environment == DeploymentEnvironment.SHADOW


def test_load_config_rejects_unknown_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "bogus")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="must be paper"):
        load_config(cells_yaml_path=yaml_path)


def test_bfx_cells_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_CELLS", "mean_reversion:fUSD_a30")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert len(cfg.cells) == 1
    assert cfg.cells[0].strategy == "mean_reversion"


def test_bfx_cells_filter_unknown_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_CELLS", "mean_reversion:fNotExist_a30")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=r"not in cells\.yaml"):
        load_config(cells_yaml_path=yaml_path)


def test_invalid_strategy_in_yaml_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    bad = _valid_yaml()
    bad["cells"][0]["strategy"] = "bogus_strategy"
    yaml_path = _write_yaml(tmp_path, bad)

    with pytest.raises(ValidationError):
        load_config(cells_yaml_path=yaml_path)


def test_mr_fust_p30_warn_but_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    bad = _valid_yaml()
    bad["cells"].append({
        "strategy": "mean_reversion",
        "symbol": "fUST",
        "period_agg": "p30",
        "timeframe": "1h",
        "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        "reference_amount_usdt": 150.0,
    })
    yaml_path = _write_yaml(tmp_path, bad)

    cfg = load_config(cells_yaml_path=yaml_path)
    assert any("unqualified" in r.message.lower() or "fust" in r.message.lower()
               for r in caplog.records)
    assert len(cfg.cells) == 3  # warn but kept


def test_load_config_uses_env_var_precedence(monkeypatch, tmp_path):
    """BFX_CELLS_YAML env var takes precedence over cwd / importlib fallback."""
    fake = tmp_path / "fake-cells.yaml"
    fake.write_text("cells: []\n")

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_CELLS_YAML", str(fake))

    from bfx_funding_bot.modules.marketfeed.config import load_config
    cfg = load_config()
    assert cfg.cells == []


def test_load_config_uses_cwd_fallback(monkeypatch, tmp_path):
    """No BFX_CELLS_YAML env → cwd / configs / cells.yaml is checked next."""
    cwd_cfg = tmp_path / "configs" / "cells.yaml"
    cwd_cfg.parent.mkdir()
    cwd_cfg.write_text("cells: []\n")

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_CELLS_YAML", raising=False)
    monkeypatch.chdir(tmp_path)

    from bfx_funding_bot.modules.marketfeed.config import load_config
    cfg = load_config()
    assert cfg.cells == []


def test_load_config_uses_importlib_resources_fallback(monkeypatch, tmp_path):
    """No env + cwd missing → importlib.resources fallback resolves real path."""
    # Create a real cells.yaml in tmp_path; mock importlib to return a
    # fake Traversable whose / "configs" / "cells.yaml" str-converts to it.
    fake_cells = tmp_path / "fake_pkg_root" / "configs" / "cells.yaml"
    fake_cells.parent.mkdir(parents=True)
    fake_cells.write_text("cells: []\n")

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_CELLS_YAML", raising=False)

    empty = tmp_path / "empty_cwd"
    empty.mkdir()
    monkeypatch.chdir(empty)

    import importlib.resources

    class _FakeRoot:
        def __init__(self, base):
            self._base = base

        def __truediv__(self, name):
            child = self._base / name
            if child.is_file():
                return child
            return _FakeRoot(child)

        def __str__(self):
            return str(self._base)

    def fake_files(pkg):
        return _FakeRoot(fake_cells.parent.parent)  # tmp_path/fake_pkg_root

    monkeypatch.setattr(importlib.resources, "files", fake_files)

    from bfx_funding_bot.modules.marketfeed.config import load_config
    cfg = load_config()
    assert cfg.cells == []


def test_load_config_env_var_set_but_path_missing_falls_through_to_cwd(monkeypatch, tmp_path):
    """BFX_CELLS_YAML pointing at non-existent file → falls through to cwd."""
    cwd_cfg = tmp_path / "configs" / "cells.yaml"
    cwd_cfg.parent.mkdir()
    cwd_cfg.write_text("cells: []\n")

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_CELLS_YAML", "/nonexistent/path/cells.yaml")
    monkeypatch.chdir(tmp_path)

    from bfx_funding_bot.modules.marketfeed.config import load_config
    cfg = load_config()
    assert cfg.cells == []


def test_load_config_raises_with_attempted_paths(monkeypatch, tmp_path):
    """All three tiers missing → FileNotFoundError lists attempted paths."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_CELLS_YAML", raising=False)

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    import importlib.resources
    def fake_files(pkg):
        class _FakeRoot:
            def __truediv__(self, _name):
                class _NoExist:
                    def is_file(self): return False
                return _NoExist()
        return _FakeRoot()
    monkeypatch.setattr(importlib.resources, "files", fake_files)

    from bfx_funding_bot.modules.marketfeed.config import load_config
    with pytest.raises(FileNotFoundError) as exc:
        load_config()
    msg = str(exc.value).lower()
    assert "bfx_cells_yaml" in msg
    assert "configs/cells.yaml" in msg
    assert "importlib" in msg or "package" in msg


def test_scheduler_buffer_s_defaults_to_30(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Bug C fix (5/20): scheduler buffer default 30s (was 5s) so p30
    candles have time to land in DB before scheduler reads."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_SCHEDULER_BUFFER_S", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert cfg.scheduler_buffer_s == 30.0


def test_scheduler_buffer_s_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_SCHEDULER_BUFFER_S", "15.5")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert cfg.scheduler_buffer_s == 15.5


def test_scheduler_buffer_s_env_invalid_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_SCHEDULER_BUFFER_S", "not-a-number")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_SCHEDULER_BUFFER_S"):
        load_config(cells_yaml_path=yaml_path)


# ---------------------------------------------------------------------------
# Phase 4.3 LOCF — staleness_budget_hours tests
# ---------------------------------------------------------------------------


def test_staleness_budget_hours_global_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry without staleness_budget_hours field → falls back to global default 2h."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_STALENESS_BUDGET_HOURS_DEFAULT", raising=False)

    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        """
cells:
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 1.0, ratio_sigma: 0.4554, ema_span: 168}
    reference_amount_usdt: 150.0
"""
    )
    cfg = load_config(cells_yaml_path=cells_yaml)

    assert len(cfg.cells) == 1
    cell = cfg.cells[0]
    assert cell.staleness_budget_hours == 2  # global default


def test_staleness_budget_hours_per_cell_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p30 entry with staleness_budget_hours: 12 → loaded as 12h; other cells fall back."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_STALENESS_BUDGET_HOURS_DEFAULT", raising=False)

    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        """
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: p30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 168}
    reference_amount_usdt: 150.0
    staleness_budget_hours: 12
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 1.0, ratio_sigma: 0.4554, ema_span: 168}
    reference_amount_usdt: 150.0
"""
    )
    cfg = load_config(cells_yaml_path=cells_yaml)

    assert cfg.cells[0].staleness_budget_hours == 12  # p30 override
    assert cfg.cells[1].staleness_budget_hours == 2   # fallback to global


def test_staleness_budget_hours_env_global_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BFX_STALENESS_BUDGET_HOURS_DEFAULT=4 → global default becomes 4h;
    per-cell yaml override (12) still wins over env."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_STALENESS_BUDGET_HOURS_DEFAULT", "4")

    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        """
cells:
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 1.0, ratio_sigma: 0.4554, ema_span: 168}
    reference_amount_usdt: 150.0
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: p30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 168}
    reference_amount_usdt: 150.0
    staleness_budget_hours: 12
"""
    )
    cfg = load_config(cells_yaml_path=cells_yaml)

    assert cfg.staleness_budget_hours_default == 4
    assert cfg.cells[0].staleness_budget_hours == 4   # env default
    assert cfg.cells[1].staleness_budget_hours == 12  # yaml override wins


def test_staleness_budget_hours_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative / zero staleness_budget_hours → ValidationError at load time."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")

    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        """
cells:
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 1.0, ratio_sigma: 0.4554, ema_span: 168}
    reference_amount_usdt: 150.0
    staleness_budget_hours: -1
"""
    )
    with pytest.raises(ValidationError, match="staleness_budget_hours"):
        load_config(cells_yaml_path=cells_yaml)


def test_staleness_budget_hours_env_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-int BFX_STALENESS_BUDGET_HOURS_DEFAULT → descriptive ValueError, not raw int() error."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_STALENESS_BUDGET_HOURS_DEFAULT", "abc")

    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        """
cells:
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 1.0, ratio_sigma: 0.4554, ema_span: 168}
    reference_amount_usdt: 150.0
"""
    )

    with pytest.raises(ValueError, match=r"BFX_STALENESS_BUDGET_HOURS_DEFAULT.*integer.*abc"):
        load_config(cells_yaml_path=cells_yaml)


def test_load_config_reads_deployment_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "shadow")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.deployment_environment == DeploymentEnvironment.SHADOW


def test_load_config_missing_deployment_environment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("BFX_DEPLOYMENT_ENV", raising=False)
    monkeypatch.delenv("BFX_CELLS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_DEPLOYMENT_ENV required"):
        load_config(cells_yaml_path=yaml_path)


def test_load_config_invalid_deployment_environment_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "staging")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_DEPLOYMENT_ENV must be one of"):
        load_config(cells_yaml_path=yaml_path)


# ---------------------------------------------------------------------------
# Phase 2 — configured_symbols co-location test
# ---------------------------------------------------------------------------


def test_configured_symbols_distinct_order_preserving():
    cells = [
        CellConfig(
            symbol="fUST",
            period_agg="a30",
            strategy="mean_reversion",
            params={"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        ),
        CellConfig(
            symbol="fUST",
            period_agg="p2",
            strategy="mean_reversion",
            params={"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        ),
        CellConfig(
            symbol="fUSD",
            period_agg="a30",
            strategy="mean_reversion",
            params={"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        ),
    ]
    assert configured_symbols(cells) == ["fUST", "fUSD"]


def test_configured_symbols_single_currency():
    cells = [
        CellConfig(
            symbol="fUST",
            period_agg="a30",
            strategy="mean_reversion",
            params={"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        ),
        CellConfig(
            symbol="fUST",
            period_agg="p2",
            strategy="mean_reversion",
            params={"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_span": 100},
        ),
    ]
    assert configured_symbols(cells) == ["fUST"]


# ---------------------------------------------------------------------------
# AdaptivePeriod strategy — enum + params validator
# ---------------------------------------------------------------------------


def _ap_params() -> dict:
    return {"ema_span": 24, "ratio_sigma": 0.42, "t1": 0.5, "t2": 1.5, "p_mid": 7, "p_long": 30}


def test_adaptive_period_enum_value() -> None:
    assert StrategyName.ADAPTIVE_PERIOD.value == "adaptive_period"


def test_cellconfig_accepts_valid_adaptive_period_params() -> None:
    c = CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=_ap_params())
    assert c.pair_id == "adaptive_period:fUST_a30"


def test_cellconfig_rejects_t2_not_greater_than_t1() -> None:
    bad = _ap_params() | {"t1": 1.5, "t2": 1.5}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)


def test_cellconfig_rejects_p_long_less_than_p_mid() -> None:
    bad = _ap_params() | {"p_mid": 30, "p_long": 7}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)


def test_cellconfig_rejects_extra_param_key() -> None:
    bad = _ap_params() | {"bogus": 1}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)


def test_cellconfig_accepts_p_long_equal_p_mid() -> None:
    ok = _ap_params() | {"p_mid": 7, "p_long": 7}
    c = CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=ok)
    assert c.strategy.value == "adaptive_period"
