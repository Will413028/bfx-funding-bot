from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.config import load_config


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
                "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_alpha": 0.02},
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


def test_load_config_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.phase == "paper"
    assert len(cfg.cells) == 2
    assert cfg.cells[0].cell_id == "fUSD_a30"


def test_load_config_rejects_canary_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="canary"):
        load_config(cells_yaml_path=yaml_path)


def test_bfx_cells_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_CELLS", "mean_reversion:fUSD_a30")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert len(cfg.cells) == 1
    assert cfg.cells[0].strategy == "mean_reversion"


def test_bfx_cells_filter_unknown_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_CELLS", "mean_reversion:fNotExist_a30")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match=r"not in cells\.yaml"):
        load_config(cells_yaml_path=yaml_path)


def test_invalid_strategy_in_yaml_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    bad = _valid_yaml()
    bad["cells"][0]["strategy"] = "bogus_strategy"
    yaml_path = _write_yaml(tmp_path, bad)

    with pytest.raises(ValidationError):
        load_config(cells_yaml_path=yaml_path)


def test_mr_fust_p30_warn_but_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    bad = _valid_yaml()
    bad["cells"].append({
        "strategy": "mean_reversion",
        "symbol": "fUST",
        "period_agg": "p30",
        "timeframe": "1h",
        "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.005, "ema_alpha": 0.02},
        "reference_amount_usdt": 150.0,
    })
    yaml_path = _write_yaml(tmp_path, bad)

    cfg = load_config(cells_yaml_path=yaml_path)
    assert any("unqualified" in r.message.lower() or "fust" in r.message.lower()
               for r in caplog.records)
    assert len(cfg.cells) == 3  # warn but kept
