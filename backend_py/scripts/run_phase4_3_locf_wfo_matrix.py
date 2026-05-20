"""Phase 4.3 LOCF sensitivity sweep — 12 WFO runs over (cell x strategy x budget) grid.

Reuses run_phase3b_wfo_matrix.py framework + applies reindex_and_ffill before each
WFO test window's run_backtest call. Emits GREEN/YELLOW/RED ship-gate verdict per
(cell, strategy) pair to docs/research/<DATE>-phase4.3-locf-backtest-results.md.

Differences from run_phase3b_wfo_matrix.py:
  - Restricted to sparse cells only: fUSD_p30, fUST_p30
  - Adds --budget-hours CLI flag (list, default 6 12 24)
  - LOCF applied: reindex_and_ffill(test_candles, ref_mts=window.test_end_mts,
      max_gap_hours=budget_hours) wraps candles before each run_backtest call
  - Non-None FilledCandle.candle entries are unwrapped for run_backtest
  - GREEN/YELLOW/RED verdict per (cell, strategy) pair based on 12h/24h qualification
  - Outputs both .md and .json to --output path

Usage:
    cd backend_py
    uv run python scripts/run_phase4_3_locf_wfo_matrix.py \\
        --budget-hours 6 12 24 \\
        --output docs/research/$(date -u +%Y-%m-%d)-phase4.3-locf-backtest-results.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.matrix import (
    evaluate_cell_qualification,
    pick_sweep_winner,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.strategies.rate_percentile import RatePercentileStrategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow, compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill

logger = logging.getLogger("phase4_3_locf_wfo_matrix")

# Only sparse cells targeted by Phase 4.3 LOCF validation
P30_CELLS: list[tuple[str, str]] = [("fUSD", "p30"), ("fUST", "p30")]
STRATEGIES: list[type[Strategy]] = [RatePercentileStrategy, MeanReversionStrategy]
DEFAULT_BUDGET_HOURS: list[int] = [6, 12, 24]
START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
TRAIN_MONTHS = 3
TEST_MONTHS = 1
STEP_MONTHS = 1

# Ship-gate thresholds (mirror Phase 3b qualification rules)
WIN_PCT_THRESHOLD = Decimal("0.60")
MARGIN_THRESHOLD = Decimal("0.05")
HEALTH_PCT_THRESHOLD = Decimal("0.80")


def apply_locf_and_unwrap(
    candles: list[FundingCandle],
    ref_mts: int,
    budget_hours: int,
) -> list[FundingCandle]:
    """Apply LOCF reindexing and unwrap non-None FilledCandle.candle entries.

    Slots where candle=None (beyond budget) are dropped — the backtest engine
    sees only valid (real or forward-filled) FundingCandle objects.
    """
    filled = reindex_and_ffill(candles, ref_mts=ref_mts, max_gap_hours=budget_hours)
    return [fc.candle for fc in filled if fc.candle is not None]


def run_cell_wfo_with_locf(
    strategy_class: type[Strategy],
    all_candles: list[FundingCandle],
    eda_cell: dict[str, Any],
    cell_key: str,
    wfo_windows: list[WfoWindow],
    budget_hours: int,
) -> tuple[list[Any], list[Any]]:
    """Run WFO for one (cell, strategy, budget_hours) with LOCF preprocessing.

    Wraps run_cell_wfo but builds LOCF-preprocessed candle views per window:
      - Train segment: raw candles (no LOCF — training on sparse data is intentional
        for robustness; LOCF only applies to runtime inference windows)
      - Test segment: reindex_and_ffill applied to simulate runtime inference
        with staleness budget = budget_hours

    Note: run_cell_wfo uses record_start/end_mts slicing internally, so we pass
    the full candle list for train (standard behaviour) but for the test window we
    substitute a pre-processed version by patching candles on a per-window basis.

    Implementation: replicate run_cell_wfo iteration inline to apply LOCF per window.
    """
    from bfx_funding_bot.modules.backtest.engine import run_backtest
    from bfx_funding_bot.modules.backtest.matrix import WindowOutcome
    from bfx_funding_bot.modules.backtest.schemas import BacktestResult
    from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy

    window_outcomes: list[WindowOutcome] = []
    baseline_results: list[BacktestResult] = []

    for w in wfo_windows:
        # Test candles: apply LOCF with this budget
        test_raw = [c for c in all_candles if w.test_start_mts <= c.mts <= w.test_end_mts]
        test_locf = apply_locf_and_unwrap(test_raw, ref_mts=w.test_end_mts, budget_hours=budget_hours)

        # Baseline uses LOCF-preprocessed test candles
        baseline_result = run_backtest(
            test_locf, AlwaysFRRStrategy(period_days=2),
            record_start_mts=w.test_start_mts,
            record_end_mts=w.test_end_mts,
        )
        baseline_results.append(baseline_result)

        try:
            grid = strategy_class.param_grid_for_cell(
                symbol=all_candles[0].symbol,
                period_agg=all_candles[0].period_agg,
                eda=eda_cell,
            )
            if not grid:
                window_outcomes.append(WindowOutcome(
                    window_idx=len(window_outcomes),
                    train_start_mts=w.train_start_mts, train_end_mts=w.train_end_mts,
                    test_start_mts=w.test_start_mts, test_end_mts=w.test_end_mts,
                    status="skipped:no_valid_candidate",
                    best_params=None,
                    oos_net=None, oos_max_dd=None, oos_fill_rate=None, oos_sortino=None,
                    baseline_net=baseline_result.net_monthly_return_pct,
                    baseline_sortino=baseline_result.sortino,
                ))
                continue

            # Train sweep: raw candles (no LOCF on training — mirrors runtime)
            candidates = []
            for params in grid:
                train_result = run_backtest(
                    all_candles, strategy_class(**params),
                    record_start_mts=w.train_start_mts,
                    record_end_mts=w.train_end_mts,
                )
                candidates.append((params, train_result))

            winner = pick_sweep_winner(candidates)
            if winner is None:
                window_outcomes.append(WindowOutcome(
                    window_idx=len(window_outcomes),
                    train_start_mts=w.train_start_mts, train_end_mts=w.train_end_mts,
                    test_start_mts=w.test_start_mts, test_end_mts=w.test_end_mts,
                    status="skipped:no_valid_candidate",
                    best_params=None,
                    oos_net=None, oos_max_dd=None, oos_fill_rate=None, oos_sortino=None,
                    baseline_net=baseline_result.net_monthly_return_pct,
                    baseline_sortino=baseline_result.sortino,
                ))
                continue

            best_params, _ = winner
            # OOS test: LOCF-preprocessed candles for this budget
            test_result = run_backtest(
                test_locf, strategy_class(**best_params),
                record_start_mts=w.test_start_mts,
                record_end_mts=w.test_end_mts,
            )
            window_outcomes.append(WindowOutcome(
                window_idx=len(window_outcomes),
                train_start_mts=w.train_start_mts, train_end_mts=w.train_end_mts,
                test_start_mts=w.test_start_mts, test_end_mts=w.test_end_mts,
                status="ok",
                best_params=best_params,
                oos_net=test_result.net_monthly_return_pct,
                oos_max_dd=test_result.max_drawdown_pct,
                oos_fill_rate=test_result.fill_rate,
                oos_sortino=test_result.sortino,
                baseline_net=baseline_result.net_monthly_return_pct,
                baseline_sortino=baseline_result.sortino,
            ))
        except Exception:
            logger.exception("Window %d errored", len(window_outcomes))
            window_outcomes.append(WindowOutcome(
                window_idx=len(window_outcomes),
                train_start_mts=w.train_start_mts, train_end_mts=w.train_end_mts,
                test_start_mts=w.test_start_mts, test_end_mts=w.test_end_mts,
                status="errored",
                best_params=None,
                oos_net=None, oos_max_dd=None, oos_fill_rate=None, oos_sortino=None,
                baseline_net=baseline_result.net_monthly_return_pct,
                baseline_sortino=baseline_result.sortino,
            ))

    return window_outcomes, baseline_results


def compute_verdicts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each (cell, strategy) pair, derive GREEN/YELLOW/RED ship-gate verdict.

    GREEN  = 12h variant qualifies (win_pct >= 60% AND margin >= 5%)
             → ship as Phase 4.4 canary candidate; cells.yaml budget stays at 12h
    YELLOW = 12h fails but 24h qualifies
             → raise cells.yaml staleness_budget_hours to 24h for this pair; re-run G2
    RED    = 24h fails
             → disqualify from Phase 4.4 canary (LOCF semantics break strategy)
    """
    pairs: dict[tuple[str, str], dict[int, bool]] = {}
    for r in results:
        key = (r["cell"], r["strategy"])
        pairs.setdefault(key, {})[r["budget_hours"]] = r["qualifies"]

    verdicts: list[dict[str, Any]] = []
    for (cell, strategy), budget_results in pairs.items():
        q6 = budget_results.get(6, False)
        q12 = budget_results.get(12, False)
        q24 = budget_results.get(24, False)
        if q12:
            verdict = "GREEN"
            action = "ship for 4.4 canary; cells.yaml budget stays at 12h"
        elif q24:
            verdict = "YELLOW"
            action = "raise cells.yaml budget for this pair to 24h; re-run G2 audit"
        else:
            verdict = "RED"
            action = "disqualify pair from 4.4 canary (LOCF semantic breaks strategy)"
        verdicts.append({
            "cell": cell,
            "strategy": strategy,
            "q6": q6,
            "q12": q12,
            "q24": q24,
            "verdict": verdict,
            "action": action,
        })
    return verdicts


def emit_markdown(
    results: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    output_path: Path,
) -> None:
    """Write per-pair x budget table + GREEN/YELLOW/RED matrix to markdown."""
    lines: list[str] = [
        "# Phase 4.3 LOCF Backtest Sensitivity Sweep Results",
        "",
        f"**Run date**: {datetime.now(UTC).isoformat()}",
        "",
        "**Sweep**: 2 cells (fUSD_p30, fUST_p30) x 2 strategies x 3 budgets = 12 runs",
        "",
        "## Per-pair x budget results",
        "",
        "| Cell | Strategy | Budget | Eligible | Wins | Win % | Margin | Health % | Qualifies |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['cell']} | {r['strategy']} | {r['budget_hours']}h | "
            f"{r['eligible']} | {r['wins']} | {float(r['win_pct']):.2%} | "
            f"{float(r['margin']):.4f} | {float(r['health_pct']):.2%} | "
            f"{'yes' if r['qualifies'] else 'no'} |"
        )

    lines.extend([
        "",
        "## Ship gate verdict per pair",
        "",
        "| Cell | Strategy | 6h | 12h | 24h | Verdict | Action |",
        "|---|---|---|---|---|---|---|",
    ])
    for v in verdicts:
        lines.append(
            f"| {v['cell']} | {v['strategy']} | "
            f"{'yes' if v['q6'] else 'no'} | "
            f"{'yes' if v['q12'] else 'no'} | "
            f"{'yes' if v['q24'] else 'no'} | "
            f"**{v['verdict']}** | {v['action']} |"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n")
    logger.info("Markdown written to %s", output_path)


def _cell_verdict_to_dict(
    cell: str,
    strategy: str,
    budget_hours: int,
    verdict: Any,  # CellVerdict
) -> dict[str, Any]:
    """Serialize CellVerdict dataclass to a plain dict for aggregation."""
    return {
        "cell": cell,
        "strategy": strategy,
        "budget_hours": budget_hours,
        "eligible": verdict.windows_eligible,
        "wins": verdict.windows_strategy_beats_baseline,
        "win_pct": str(verdict.pct_windows_won),
        "margin": str(verdict.relative_margin),
        "health_pct": str(verdict.health_pct),
        "qualifies": verdict.qualifies,
    }


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Parse cells: "fUSD_p30" → ("fUSD", "p30")
    parsed_cells: list[tuple[str, str]] = []
    for cell_str in args.cells:
        parts = cell_str.split("_", 1)
        if len(parts) != 2:
            logger.error("Invalid cell format (expected symbol_period_agg): %s", cell_str)
            return 1
        parsed_cells.append((parts[0], parts[1]))

    # Parse strategies
    strategy_map: dict[str, type[Strategy]] = {
        "rate_percentile": RatePercentileStrategy,
        "mean_reversion": MeanReversionStrategy,
    }
    strategy_classes: list[type[Strategy]] = []
    for s in args.strategies:
        if s not in strategy_map:
            logger.error("Unknown strategy: %s (choices: %s)", s, list(strategy_map))
            return 1
        strategy_classes.append(strategy_map[s])

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    results: list[dict[str, Any]] = []

    try:
        for symbol, period_agg in parsed_cells:
            cell_key = f"{symbol}_{period_agg}"
            print(f"\n## Cell: {cell_key}")

            async with session_scope(session_factory) as session:
                all_candles = await get_candles_in_range(
                    session,
                    symbol=symbol,
                    timeframe="1h",
                    period_agg=period_agg,
                    start_mts=START_MTS,
                    end_mts=end_mts,
                )
            if len(all_candles) < 720:
                print(f"  SKIP: len(candles)={len(all_candles)} < 720")
                continue

            windows = compute_wfo_windows(
                all_candles,
                train_months=TRAIN_MONTHS,
                test_months=TEST_MONTHS,
                step_months=STEP_MONTHS,
            )
            print(f"  n_candles: {len(all_candles)}; n_wfo_windows: {len(windows)}")
            if not windows:
                print("  SKIP: no WFO windows survived min-candles filter")
                continue

            # Minimal EDA for param_grid_for_cell (Phase 3b used a pre-computed EDA JSON;
            # Phase 4.3 sensitivity sweep runs without pre-computed EDA — pass empty dict
            # which strategies handle gracefully by using default ranges)
            eda_cell: dict[str, Any] = {}

            for strategy_class in strategy_classes:
                for budget_hours in args.budget_hours:
                    print(
                        f"  Running {strategy_class.__name__} x budget={budget_hours}h ...",
                        flush=True,
                    )
                    window_outcomes, _ = run_cell_wfo_with_locf(
                        strategy_class=strategy_class,
                        all_candles=all_candles,
                        eda_cell=eda_cell,
                        cell_key=cell_key,
                        wfo_windows=windows,
                        budget_hours=budget_hours,
                    )
                    verdict = evaluate_cell_qualification(window_outcomes)
                    row = _cell_verdict_to_dict(
                        cell=cell_key,
                        strategy=strategy_class.__name__,
                        budget_hours=budget_hours,
                        verdict=verdict,
                    )
                    results.append(row)
                    print(
                        f"    eligible={verdict.windows_eligible}, "
                        f"wins={verdict.windows_strategy_beats_baseline} "
                        f"({verdict.pct_windows_won:.2%}), "
                        f"margin={verdict.relative_margin:.3f}, "
                        f"health={verdict.health_pct:.2%}, "
                        f"qualifies={verdict.qualifies}"
                    )

        verdicts = compute_verdicts(results)

        print("\n## Ship-gate verdicts\n")
        for v in verdicts:
            print(
                f"  {v['cell']} x {v['strategy']}: "
                f"6h={'Y' if v['q6'] else 'N'} "
                f"12h={'Y' if v['q12'] else 'N'} "
                f"24h={'Y' if v['q24'] else 'N'} "
                f"→ {v['verdict']} — {v['action']}"
            )

        output_path = args.output
        json_path = output_path.with_suffix(".json")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({"results": results, "verdicts": verdicts}, indent=2))
        emit_markdown(results, verdicts, output_path)
        print(f"\nResults written to {output_path} + {json_path}")
        return 0

    except Exception:
        logger.exception("Sweep failed")
        return 2
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--cells",
        nargs="+",
        default=["fUSD_p30", "fUST_p30"],
        help="Cells to sweep in symbol_period_agg format (default: fUSD_p30 fUST_p30)",
    )
    p.add_argument(
        "--strategies",
        nargs="+",
        default=["rate_percentile", "mean_reversion"],
        choices=["rate_percentile", "mean_reversion"],
        help="Strategies to sweep (default: rate_percentile mean_reversion)",
    )
    p.add_argument(
        "--budget-hours",
        nargs="+",
        type=int,
        default=DEFAULT_BUDGET_HOURS,
        metavar="N",
        help="LOCF staleness budgets in hours (default: 6 12 24)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/phase4.3_locf_results.md"),
        help="Output markdown path (also writes .json alongside)",
    )
    return p.parse_args()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
