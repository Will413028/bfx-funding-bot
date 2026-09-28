"""Phase 3b-WFO matrix runner.

Sweeps 2 strategies (RatePercentile, MeanReversion) over 6 cells
(fUSD/fUST x p2/p30/a30), running walk-forward (3-month train + 1-month
test, 1-month walk step) over post-2022 candles loaded from Neon.

Outputs per-cell window outcomes + per-cell verdicts + per-strategy
verdicts to stdout for review and copy into the results report.

Usage:
    cd backend
    uv run python scripts/run_phase3b_wfo_matrix.py \\
        --eda /path/to/eda.json \\
        > /tmp/phase3b_wfo_results.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

from bfx_funding_bot.apps.research import research_strategy
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.matrix import (
    CellVerdict,
    evaluate_cell_qualification,
    evaluate_strategy_qualification,
    run_cell_wfo,
)
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.strategy import ResearchStrategySpec

logger = logging.getLogger("phase3b_wfo_matrix")

SYMBOLS = ["fUSD", "fUST"]
PERIOD_AGGS = ["p2", "p30", "a30"]
START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
STRATEGIES: list[ResearchStrategySpec] = [
    research_strategy("RatePercentileStrategy"), research_strategy("MeanReversionStrategy"),
]
BASELINE = research_strategy("AlwaysMarketRateStrategy")
TRAIN_MONTHS = 3
TEST_MONTHS = 1
STEP_MONTHS = 1
RESEARCH_CONFIG = BacktestConfig(fill_model="linear-baseline")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eda", required=True, type=Path,
                   help="Path to EDA JSON (per-cell dict of stats)")
    return p.parse_args()


def _eda_for_cell(eda_blob: dict[str, Any], cell_key: str) -> dict[str, Any]:
    raw: dict[str, Any] = eda_blob.get(cell_key) or {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, (int, float, str)):
            try:
                out[k] = Decimal(str(v))
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    eda_blob = json.loads(args.eda.read_text())

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    end_mts = int(datetime.now(UTC).timestamp() * 1000)

    per_strategy_cells: dict[str, list[CellVerdict]] = {sc.__name__: [] for sc in STRATEGIES}

    try:
        for symbol, period_agg in product(SYMBOLS, PERIOD_AGGS):
            cell_key = f"{symbol}_{period_agg}"
            print(f"\n## Cell: {cell_key}")

            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session, symbol=symbol, timeframe="1h",
                    period_agg=period_agg,
                    start_mts=START_MTS, end_mts=end_mts,
                )
            if len(candles) < 720:
                print(f"- SKIP: len(candles)={len(candles)} < 720")
                continue

            windows = compute_wfo_windows(
                candles,
                train_months=TRAIN_MONTHS,
                test_months=TEST_MONTHS,
                step_months=STEP_MONTHS,
            )
            print(f"- n_candles: {len(candles)}; n_wfo_windows: {len(windows)}")
            if not windows:
                print("- SKIP: no WFO windows survived min-candles filter")
                continue

            eda_cell = _eda_for_cell(eda_blob, cell_key)

            for strategy_spec in STRATEGIES:
                outcomes, _baselines = run_cell_wfo(
                    strategy_spec=strategy_spec,
                    candles=candles,
                    eda_cell=eda_cell,
                    cell_key=cell_key,
                    wfo_windows=windows,
                    config=RESEARCH_CONFIG,
                    fill_model=None,
                    baseline=BASELINE,
                )
                verdict = evaluate_cell_qualification(outcomes)
                per_strategy_cells[strategy_spec.name].append(verdict)
                model = _baselines[0] if _baselines else None

                print(
                    f"- {strategy_spec.name}: "
                    f"eligible={verdict.windows_eligible}, "
                    f"wins={verdict.windows_strategy_beats_baseline} "
                    f"({verdict.pct_windows_won:.2%}), "
                    f"margin={verdict.relative_margin:.3f}, "
                    f"health={verdict.health_pct:.2%}, "
                    f"incomplete={verdict.incomplete_windows}, "
                    f"qualifies={verdict.qualifies}"
                )
                print(
                    f"  model_kind={model.model_kind if model else RESEARCH_CONFIG.fill_model} "
                    f"model_version={model.model_version if model else None} "
                    f"artifact_hash={model.artifact_hash if model else None} "
                    f"cutoff_ms={model.model_cutoff_ms if model else None} "
                    f"sample_count={model.model_sample_count if model else None} "
                    f"incomplete_reasons={[o.incomplete_reason for o in outcomes if o.incomplete_reason]}"
                )

        print("\n## Strategy-level verdicts\n")
        for strategy_spec in STRATEGIES:
            cells = per_strategy_cells[strategy_spec.name]
            sverdict = evaluate_strategy_qualification(cells)
            print(
                f"- {strategy_spec.name}: "
                f"{sverdict.cells_qualifying}/{sverdict.cells_played} cells qualify, "
                f"incomplete_cells={sverdict.incomplete_cells}, "
                f"Phase 4 candidate = {sverdict.qualifies}"
            )

        return 0
    except Exception:
        logger.exception("Matrix run failed")
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
