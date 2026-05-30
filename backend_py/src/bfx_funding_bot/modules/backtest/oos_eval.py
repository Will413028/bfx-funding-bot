"""Fixed-param rolling out-of-sample evaluation.

For each WFO window: slice candles to [train_start, test_end] (EMA warmup +
test month), run the strategy and the AlwaysFRR baseline recording only the
test month, and emit one paired WindowOutcome each. Params are FIXED (no
per-window re-fit) -- this is the evaluation the deploy gate, the derivation
sweep, and the OOS research script all share. No DB.
"""
from __future__ import annotations

from collections.abc import Callable

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import AlwaysMarketRateStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _outcome(result: BacktestResult, month_mts: int) -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts,
        net_monthly=result.net_monthly_return_pct,
        n_trades=result.n_trades,
        fill_rate=result.fill_rate,
    )


def evaluate_oos_windows(
    candles: list[FundingCandle],
    windows: list[WfoWindow],
    make_strategy: Callable[[], Strategy],
    *,
    config: BacktestConfig | None = None,
    baseline_period_days: int = 2,
) -> tuple[list[WindowOutcome], list[WindowOutcome]]:
    """Run a fixed-param strategy and AlwaysFRR over rolling test months.

    `make_strategy` is called once per window (strategy state is per-window:
    fresh EMA warmed only on that window's slice -> no cross-window leakage).
    Returns (strat_outcomes, base_outcomes), aligned 1:1 by window order.

    Args:
        candles: Full candle series covering all windows.
        windows: Walk-forward windows. Each window's candles are sliced to
            [train_start_mts, test_end_mts] so state warms up on the train
            portion before recording only the test month.
        make_strategy: Factory called once per window to produce a fresh
            strategy instance (prevents cross-window state leakage).
        config: BacktestConfig to use. Defaults to linear fill model to match
            the OOS research script (run_oos_profitability.py).
        baseline_period_days: period_days for AlwaysMarketRateStrategy. Default 2.
    """
    # Construct inside the function to avoid shared mutable default state.
    # "linear" matches the OOS research script (run_oos_profitability.py) which
    # also uses BacktestConfig(fill_model="linear") for deterministic results.
    effective_config = config if config is not None else BacktestConfig(fill_model="linear")

    strat_outcomes: list[WindowOutcome] = []
    base_outcomes: list[WindowOutcome] = []
    for w in windows:
        sliced = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
        rs = run_backtest(sliced, make_strategy(), effective_config, w.test_start_mts, w.test_end_mts)
        rb = run_backtest(
            sliced, AlwaysMarketRateStrategy(period_days=baseline_period_days),
            effective_config, w.test_start_mts, w.test_end_mts,
        )
        strat_outcomes.append(_outcome(rs, w.test_start_mts))
        base_outcomes.append(_outcome(rb, w.test_start_mts))
    return strat_outcomes, base_outcomes
