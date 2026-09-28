"""Fixed-param rolling out-of-sample evaluation.

For each WFO window: slice candles to [train_start, test_end] (EMA warmup +
test month), run the strategy and the AlwaysMarketRate baseline recording only the
test month, and emit one paired WindowOutcome each. Params are FIXED (no
per-window re-fit) -- this is the evaluation the deploy gate, the derivation
sweep, and the OOS research script all share. No DB.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import BacktestIncomplete, run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel
from bfx_funding_bot.modules.strategy import AlwaysMarketRateStrategy, Strategy


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
    config: BacktestConfig,
    fill_model: FillRateModel | None,
    baseline_period_days: int = 2,
    market_candles: list[FundingCandle] | None = None,
) -> tuple[list[WindowOutcome], list[WindowOutcome]]:
    """Run a fixed-param strategy and AlwaysMarketRate over rolling test months.

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
        config: Explicit fill/friction model configuration.
        fill_model: Explicit versioned empirical model, or None only for the
            deliberately selected linear-baseline model.
        baseline_period_days: period_days for AlwaysMarketRateStrategy. Default 2.
        market_candles: Optional true-market series for pricing fills, when
            `candles` is a distorted view of what the strategy saw. Sliced to the
            same window. Defaults to `candles` (unchanged behaviour).
    """
    strat_outcomes: list[WindowOutcome] = []
    base_outcomes: list[WindowOutcome] = []
    for w in windows:
        sliced = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
        sliced_market = (
            [c for c in market_candles if w.train_start_mts <= c.mts <= w.test_end_mts]
            if market_candles is not None
            else None
        )
        try:
            rs = run_backtest(
                sliced, make_strategy(), config,
                w.test_start_mts, w.test_end_mts,
                fill_model=fill_model,
                market_candles=sliced_market,
            )
            rb = run_backtest(
                sliced, AlwaysMarketRateStrategy(period_days=baseline_period_days),
                config, w.test_start_mts, w.test_end_mts,
                fill_model=fill_model,
                market_candles=sliced_market,
            )
        except BacktestIncomplete as error:
            incomplete = WindowOutcome(
                month_mts=w.test_start_mts,
                net_monthly=Decimal("0"),
                n_trades=0,
                fill_rate=Decimal("0"),
                status="incomplete",
                incomplete_reason=error.reason,
            )
            strat_outcomes.append(incomplete)
            base_outcomes.append(incomplete)
            continue
        strat_outcomes.append(_outcome(rs, w.test_start_mts))
        base_outcomes.append(_outcome(rb, w.test_start_mts))
    return strat_outcomes, base_outcomes
