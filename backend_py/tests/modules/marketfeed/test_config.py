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


def test_load_config_uses_env_var_precedence(monkeypatch, tmp_path):
    """BFX_CELLS_YAML env var takes precedence over cwd / importlib fallback."""
    fake = tmp_path / "fake-cells.yaml"
    fake.write_text("cells: []\n")

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "k"); monkeypatch.setenv("AXIOM_DATASET", "d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
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
    monkeypatch.setenv("AXIOM_API_KEY", "k"); monkeypatch.setenv("AXIOM_DATASET", "d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
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
    monkeypatch.setenv("AXIOM_API_KEY", "k"); monkeypatch.setenv("AXIOM_DATASET", "d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
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
    monkeypatch.setenv("AXIOM_API_KEY", "k"); monkeypatch.setenv("AXIOM_DATASET", "d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("BFX_CELLS_YAML", "/nonexistent/path/cells.yaml")
    monkeypatch.chdir(tmp_path)

    from bfx_funding_bot.modules.marketfeed.config import load_config
    cfg = load_config()
    assert cfg.cells == []


def test_load_config_raises_with_attempted_paths(monkeypatch, tmp_path):
    """All three tiers missing → FileNotFoundError lists attempted paths."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "k"); monkeypatch.setenv("AXIOM_DATASET", "d")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("BFX_CELLS_YAML", raising=False)

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    import importlib.resources
    def fake_files(pkg):
        class _FakeRoot:
            def __truediv__(self, _name):
                class _NoExist:
                    def is_file(self_inner): return False
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
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.delenv("BFX_SCHEDULER_BUFFER_S", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert cfg.scheduler_buffer_s == 30.0


def test_scheduler_buffer_s_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_SCHEDULER_BUFFER_S", "15.5")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)
    assert cfg.scheduler_buffer_s == 15.5


def test_scheduler_buffer_s_env_invalid_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_SCHEDULER_BUFFER_S", "not-a-number")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="BFX_SCHEDULER_BUFFER_S"):
        load_config(cells_yaml_path=yaml_path)
