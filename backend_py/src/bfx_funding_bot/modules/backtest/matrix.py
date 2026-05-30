"""Phase 3b-WFO matrix helpers: sweep-winner selection + WFO qualification rules.

Pure-function module. Loaded by the WFO matrix runner script
(scripts/run_phase3b_wfo_matrix.py).

run_cell_wfo orchestration helper added in next task (WFO Task 5).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import AlwaysMarketRateStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle

FILL_FLOOR = Decimal("0.3")
MIN_TRADES_TRAIN = 10
CONSISTENCY_THRESHOLD = Decimal("0.60")
MARGIN_THRESHOLD = Decimal("0.05")
HEALTH_THRESHOLD = Decimal("0.80")
DEFAULT_TOTAL_CELLS = 6
DEFAULT_CELLS_REQUIRED = 4


def pick_sweep_winner(
    candidates: list[tuple[dict[str, Any], BacktestResult]],
) -> tuple[dict[str, Any], BacktestResult] | None:
    """Sweep tie-break:
    1. Drop candidates with fill_rate < 0.3 OR n_trades < 10.
    2. If any candidate has sortino == +Infinity, pick within the +inf
       subset by max net_monthly_return_pct.
    3. Otherwise pick by max sortino.
    Returns None when all candidates are filtered.
    """
    eligible = [
        (p, r) for p, r in candidates
        if r.fill_rate >= FILL_FLOOR and r.n_trades >= MIN_TRADES_TRAIN
    ]
    if not eligible:
        return None
    inf_subset = [(p, r) for p, r in eligible if r.sortino == Decimal("Infinity")]
    if inf_subset:
        return max(inf_subset, key=lambda pr: pr[1].net_monthly_return_pct)
    return max(eligible, key=lambda pr: pr[1].sortino)


@dataclass(frozen=True)
class WindowOutcome:
    """One WFO window's outcome for a single (strategy, cell) pair."""
    window_idx: int
    train_start_mts: int
    train_end_mts: int
    test_start_mts: int
    test_end_mts: int
    status: str  # "ok" | "skipped:no_valid_candidate" | "errored"
    best_params: dict[str, Any] | None
    oos_net: Decimal | None
    oos_max_dd: Decimal | None
    oos_fill_rate: Decimal | None
    oos_sortino: Decimal | None
    baseline_net: Decimal | None
    baseline_sortino: Decimal | None


@dataclass(frozen=True)
class CellVerdict:
    """Aggregate of WFO windows for one (strategy, cell) pair."""
    qualifies: bool
    windows_eligible: int  # status == "ok"
    windows_strategy_beats_baseline: int
    pct_windows_won: Decimal
    mean_strategy_net: Decimal
    mean_baseline_net: Decimal
    relative_margin: Decimal  # (mean_strat - mean_base) / mean_base
    health_pct: Decimal       # fraction of eligible windows with fill+trades floors met


@dataclass(frozen=True)
class StrategyVerdict:
    """Aggregate of CellVerdicts for one strategy across all cells."""
    qualifies: bool
    cells_qualifying: int
    cells_played: int


def evaluate_cell_qualification(
    window_outcomes: list[WindowOutcome],
    consistency_threshold: Decimal = CONSISTENCY_THRESHOLD,
    margin_threshold: Decimal = MARGIN_THRESHOLD,
    health_threshold: Decimal = HEALTH_THRESHOLD,
) -> CellVerdict:
    """Decide whether a strategy qualifies on a cell, given per-window outcomes.

    Eligible windows = those with status == "ok". Skipped/errored windows
    are excluded from the consistency denominator.

    Qualification = all of:
      - pct_windows_won >= consistency_threshold (default 60%)
      - relative_margin > margin_threshold (default 5%)
      - health_pct >= health_threshold (default 80%)
    """
    eligible = [o for o in window_outcomes if o.status == "ok"]
    if not eligible:
        return CellVerdict(
            qualifies=False, windows_eligible=0,
            windows_strategy_beats_baseline=0,
            pct_windows_won=Decimal("0"),
            mean_strategy_net=Decimal("0"), mean_baseline_net=Decimal("0"),
            relative_margin=Decimal("0"), health_pct=Decimal("0"),
        )

    n = Decimal(len(eligible))
    wins = sum(
        1 for o in eligible
        if o.oos_net is not None and o.baseline_net is not None and o.oos_net > o.baseline_net
    )
    pct_won = Decimal(wins) / n

    strat_nets = [o.oos_net for o in eligible if o.oos_net is not None]
    base_nets = [o.baseline_net for o in eligible if o.baseline_net is not None]
    mean_strat = sum(strat_nets, Decimal("0")) / Decimal(len(strat_nets)) if strat_nets else Decimal("0")
    mean_base = sum(base_nets, Decimal("0")) / Decimal(len(base_nets)) if base_nets else Decimal("0")
    margin = (mean_strat - mean_base) / mean_base if mean_base > 0 else Decimal("0")

    healthy = sum(
        1 for o in eligible
        if o.oos_fill_rate is not None and o.oos_fill_rate >= FILL_FLOOR
    )
    health_pct = Decimal(healthy) / n

    pass_consistency = pct_won >= consistency_threshold
    pass_margin = margin > margin_threshold
    pass_health = health_pct >= health_threshold

    return CellVerdict(
        qualifies=pass_consistency and pass_margin and pass_health,
        windows_eligible=len(eligible),
        windows_strategy_beats_baseline=wins,
        pct_windows_won=pct_won,
        mean_strategy_net=mean_strat,
        mean_baseline_net=mean_base,
        relative_margin=margin,
        health_pct=health_pct,
    )


def evaluate_strategy_qualification(
    per_cell_verdicts: list[CellVerdict],
    cells_required: int = DEFAULT_CELLS_REQUIRED,
    total_cells: int = DEFAULT_TOTAL_CELLS,
) -> StrategyVerdict:
    """Strategy qualifies for Phase 4 candidate pool when it qualifies on
    >= cells_required cells out of total_cells.
    """
    cells_qualifying = sum(1 for v in per_cell_verdicts if v.qualifies)
    cells_played = len(per_cell_verdicts)
    return StrategyVerdict(
        qualifies=cells_qualifying >= cells_required,
        cells_qualifying=cells_qualifying,
        cells_played=cells_played,
    )


def run_cell_wfo(
    strategy_class: type[Strategy],
    candles: list[FundingCandle],
    eda_cell: dict[str, Any],
    cell_key: str,
    wfo_windows: list[WfoWindow],
) -> tuple[list[WindowOutcome], list[BacktestResult]]:
    """Run sweep + OOS eval for one (strategy, cell) across all WFO windows.

    For each window:
      1. Run baseline (AlwaysFRR period=2) over the test segment.
      2. Build the strategy's param grid via param_grid_for_cell(eda_cell).
      3. Sweep each variant on the train segment; filter via pick_sweep_winner.
      4. If a winner exists, run OOS on the test segment with the winning params.
      5. Record WindowOutcome (status = "ok" | "skipped:no_valid_candidate" | "errored").

    Returns (window_outcomes, baseline_per_window).
    """
    window_outcomes: list[WindowOutcome] = []
    baseline_results: list[BacktestResult] = []

    for w in wfo_windows:
        baseline_result = run_backtest(
            candles, AlwaysMarketRateStrategy(period_days=2),
            record_start_mts=w.test_start_mts,
            record_end_mts=w.test_end_mts,
        )
        baseline_results.append(baseline_result)

        try:
            grid = strategy_class.param_grid_for_cell(
                symbol=candles[0].symbol, period_agg=candles[0].period_agg,
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

            candidates = []
            for params in grid:
                train_result = run_backtest(
                    candles, strategy_class(**params),
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
            test_result = run_backtest(
                candles, strategy_class(**best_params),
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
