"""Offline pipeline logic for derive_cells: freeze fixtures, patch YAML params
+ _provenance, and the drift check. No network. The Neon pull + CLI live in
scripts/derive_cells.py. See docs/superpowers/specs/2026-05-28-tier2-deployment-safety-design.md.
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
from bfx_funding_bot.modules.marketfeed.config import load_cells_only

CONFIGS = Path("configs")
CELLS_YAML = CONFIGS / "cells.yaml"
CANARY_YAML = CONFIGS / "cells.canary.yaml"
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


def _mr_cells_in(path: Path) -> dict[CellKey, dict[str, Any]]:
    """Map (strategy, symbol, period_agg) -> params dict for MR cells in a YAML."""
    out: dict[CellKey, dict[str, Any]] = {}
    for c in load_cells_only(path):
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
    canary_path: Path | None,
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
        # _provenance only in the full cells.yaml, not the canary subset.
        if canary_path is None or path != canary_path:
            doc["_provenance"] = provenance
        with path.open("w") as fh:
            ruamel.dump(doc, fh)


def check_against_fixture(
    fixtures_dir: Path,
    yaml_paths: list[Path],
    *,
    canary_path: Path | None,
    config: BacktestConfig,
    fill_model: FillRateModel,
) -> list[str]:
    """Offline drift check. Returns a list of human-readable problems ([] = OK)."""
    problems: list[str] = []

    # Identify the main (non-canary) YAML file.
    main = next(
        (p for p in yaml_paths if canary_path is None or p != canary_path),
        yaml_paths[0],
    )
    committed = _mr_cells_in(main)

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
        return problems

    # 2. Re-derive from the frozen fixtures (only MR cells whose fixture exists).
    derived: dict[CellKey, DerivedCell] = {}
    for symbol, period_agg, timeframe in MR_CELLS:
        fpath = fixtures_dir / f"{symbol}_{period_agg}_{timeframe}.jsonl.gz"
        if not fpath.exists():
            continue
        derived[("mean_reversion", symbol, period_agg)] = derive_cell_params(
            load_candles(fpath), config=config, fill_model=fill_model
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

    # 4. Canary params must match the main file for shared MR cells.
    if canary_path is not None and canary_path.exists():
        canary = _mr_cells_in(canary_path)
        for key, cparams in canary.items():
            mparams = committed.get(key)
            if mparams is None:
                problems.append(f"canary cell {key} not in {main.name}")
                continue
            for field in ("ema_span", "threshold_sigma", "ratio_sigma"):
                cgot = cparams.get(field)
                mgot = mparams.get(field)
                if cgot is None or mgot is None or abs(float(cgot) - float(mgot)) > 1e-9:
                    problems.append(
                        f"canary {key[1]}_{key[2]} {field} != {main.name}"
                    )
    return problems
