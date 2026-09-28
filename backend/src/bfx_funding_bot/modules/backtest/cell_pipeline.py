"""Offline pipeline logic for derive_cells: freeze fixtures, patch YAML params
+ _provenance, and the drift check. No network. The Neon pull + CLI live in
scripts/derive_cells.py.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

from bfx_funding_bot.modules.backtest.cell_derivation import DerivedCell, derive_cell_params
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.fixture_io import (
    fixture_data_hash,
    freeze_candles,
    load_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel
from bfx_funding_bot.modules.strategy import CellConfig, ResearchStrategySpec

CONFIGS = Path("configs")
CELLS_YAML = CONFIGS / "cells.yaml"
DEPLOYED_YAML = CONFIGS / "cells.live.yaml"
FIXTURES = Path("fixtures/candles")

# MeanReversion cells to derive (RatePercentile is static, not EDA-derived).
# Items are (symbol, period_agg, timeframe) — NOT CellKey-shaped.
MR_CELLS: list[tuple[str, str, str]] = [
    ("fUST", "a30", "1h"),
    ("fUST", "p2", "1h"),
    ("fUSD", "a30", "1h"),
    ("fUSD", "p2", "1h"),
    ("fUSD", "p30", "1h"),
]

CellKey = tuple[str, str, str]  # (strategy, symbol, period_agg)


def _mr_cells_in(cells: tuple[CellConfig, ...]) -> dict[CellKey, dict[str, Any]]:
    """Map (strategy, symbol, period_agg) -> params dict for MR cells."""
    out: dict[CellKey, dict[str, Any]] = {}
    for c in cells:
        if c.strategy.value == "mean_reversion":
            key: CellKey = (c.strategy.value, c.symbol, c.period_agg)
            out[key] = dict(c.params)
    return out


def write_outputs(
    series: dict[CellKey, list[FundingCandle]],
    derived: dict[CellKey, DerivedCell],
    fixtures_dir: Path,
    yaml_paths: list[Path],
    *,
    deployed_path: Path | None,
) -> None:
    """Freeze fixtures and patch params + _provenance into each YAML (ruamel
    round-trip preserves comments). Deterministic; only touches disk."""
    # series keyed by (symbol, period_agg, timeframe) for fixture_io
    freeze_candles(
        {(s, p, "1h"): cs for (_strat, s, p), cs in series.items()},
        fixtures_dir,
    )
    data_hash = fixture_data_hash(fixtures_dir)

    ruamel = YAML()
    ruamel.preserve_quotes = True
    # Match the existing config style (block list `  - ` at offset 2) so a
    # param patch is a minimal diff, not a whole-file re-indent.
    ruamel.indent(mapping=2, sequence=4, offset=2)
    # Keep flow-style `params: {...}` maps on one line (default width=80 wraps
    # the longest ratio_sigma onto a continuation line).
    ruamel.width = 4096

    prov_cells: dict[str, dict[str, Any]] = {}
    for (_strat, _sym, _pa), d in derived.items():
        prov_cells[f"{d.symbol}/{d.period_agg}"] = {
            "ema_span": d.ema_span,
            "threshold_sigma": float(d.threshold_sigma),
            "ratio_sigma": float(d.ratio_sigma),
            "mean_active": float(d.mean_active),
            "information_ratio": float(d.information_ratio),
            "pct_outperform": float(d.pct_outperform),
            "n_windows": d.n_windows,
        }
    provenance: dict[str, Any] = {
        "data_hash": data_hash,
        "derived_at": datetime.now(UTC).isoformat(),
        "fixture_window": {
            "start": "2022-01-01",
            "end": datetime.now(UTC).date().isoformat(),
        },
        "cells": prov_cells,
    }

    for path in yaml_paths:
        doc = ruamel.load(path.read_text())
        for cell in doc.get("cells", []):
            if cell.get("strategy") != "mean_reversion":
                continue
            cell_d = derived.get(("mean_reversion", cell["symbol"], cell["period_agg"]))
            if cell_d is None:
                continue
            cell["params"]["ema_span"] = cell_d.ema_span
            cell["params"]["threshold_sigma"] = float(cell_d.threshold_sigma)
            cell["params"]["ratio_sigma"] = float(cell_d.ratio_sigma)
        # _provenance only in the full cells.yaml, not the deployed subset.
        if deployed_path is None or path != deployed_path:
            doc["_provenance"] = provenance
        with path.open("w") as fh:
            ruamel.dump(doc, fh)


def check_main_against_fixture(
    fixtures_dir: Path,
    main: Path,
    main_cells: tuple[CellConfig, ...],
    *,
    config: BacktestConfig,
    fill_model: FillRateModel,
    strategy_spec: ResearchStrategySpec,
    baseline: ResearchStrategySpec,
) -> tuple[list[str], bool]:
    """Check the main YAML and fixtures; bool says whether deployed comparison follows."""
    problems: list[str] = []
    committed = _mr_cells_in(main_cells)

    # 1. _provenance.data_hash must match the on-disk fixtures (checked first so
    #    a corrupted/mutated fixture is caught before re-derivation attempts).
    raw = yaml.safe_load(main.read_text())
    prov: dict[str, Any] = raw.get("_provenance", {})
    on_disk = fixture_data_hash(fixtures_dir)
    if prov.get("data_hash") != on_disk:
        problems.append(
            f"_provenance.data_hash {prov.get('data_hash')!r} != "
            f"on-disk fixture hash {on_disk!r}"
        )
        # Hash mismatch means fixture and provenance are inconsistent; skip
        # re-derivation which would operate on untrusted data.
        return problems, False

    # 2. Re-derive from the frozen fixtures (only MR cells whose fixture exists).
    derived: dict[CellKey, DerivedCell] = {}
    for symbol, period_agg, timeframe in MR_CELLS:
        fpath = fixtures_dir / f"{symbol}_{period_agg}_{timeframe}.jsonl.gz"
        if not fpath.exists():
            continue
        derived[("mean_reversion", symbol, period_agg)] = derive_cell_params(
            load_candles(fpath), config=config, fill_model=fill_model,
            strategy_spec=strategy_spec, baseline=baseline,
        )

    # 3. Committed params must equal the re-derivation.
    for key, d in derived.items():
        params = committed.get(key)
        if params is None:
            continue  # cell not deployed in this file; fine
        for field, want in (
            ("ema_span", d.ema_span),
            ("threshold_sigma", float(d.threshold_sigma)),
            ("ratio_sigma", float(d.ratio_sigma)),
        ):
            got = params.get(field)
            # Values are exact YAML float round-trips (ruamel uses repr), so a
            # matching cell diffs by 0.0; 1e-9 is a generous guard, not a budget.
            if got is None or abs(float(got) - float(want)) > 1e-9:
                problems.append(
                    f"{key[1]}_{key[2]} {field}: committed {got!r} != derived {want!r}"
                )

    return problems, True


def check_deployed_cells(
    main_cells: tuple[CellConfig, ...],
    deployed_cells: tuple[CellConfig, ...],
    main_name: str,
) -> list[str]:
    """Compare deployed MR params with the already checked main cells."""
    problems: list[str] = []
    committed = _mr_cells_in(main_cells)
    deployed = _mr_cells_in(deployed_cells)
    for key, cparams in deployed.items():
        mparams = committed.get(key)
        if mparams is None:
            problems.append(f"deployed cell {key} not in {main_name}")
            continue
        for field in ("ema_span", "threshold_sigma", "ratio_sigma"):
            cgot = cparams.get(field)
            mgot = mparams.get(field)
            if cgot is None or mgot is None or abs(float(cgot) - float(mgot)) > 1e-9:
                problems.append(
                    f"deployed {key[1]}_{key[2]} {field} != {main_name}"
                )
    return problems
