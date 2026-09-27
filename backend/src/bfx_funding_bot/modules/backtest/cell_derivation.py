"""Reproducible per-cell MeanReversion parameter derivation.

EDA (close/EMA sigma on the train portion) -> the existing param grid ->
fixed-combo rolling-OOS evaluation per combo -> select the combo that beats
the passive baseline most, among those that pass the deploy gate's
distinguishability filter. Replaces the hand-picked middle-of-grid config.

Tier 3 (deferred): plateau/robustness selection -- this picks the single best
point.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.eda import close_over_ema_sigma
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import active_return_summary
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

# A scored combo: (params, mean_active, information_ratio, pct_months_outperform)
ScoredCombo = tuple[dict[str, Any], Decimal, Decimal, Decimal]


class NoDistinguishableComboError(Exception):
    """No grid combo acts differently from passive.

    Means MeanReversion has no edge on this cell. Caller (derive_cells --write)
    must fail loudly, not ship an inert config.
    """


@dataclass(frozen=True)
class DerivedCell:
    symbol: str
    period_agg: str
    timeframe: str
    ema_span: int
    threshold_sigma: Decimal
    ratio_sigma: Decimal
    mean_active: Decimal       # winner's mean active return (%/mo)
    information_ratio: Decimal
    pct_outperform: Decimal
    n_windows: int


def select_winner(scored: list[ScoredCombo]) -> dict[str, Any]:
    """Pick the params with max mean_active among distinguishable combos.

    Distinguishable = IR != 0 and pct_outperform > 0 (same condition as
    deploy_gate.evaluate_gate's distinguishable criterion).
    Stable tie-break: smallest (ema_span, threshold_sigma).
    Raises NoDistinguishableComboError if none are distinguishable.
    """
    eligible = [
        (params, ma)
        for (params, ma, ir, pct) in scored
        if ir != Decimal("0") and pct > Decimal("0")
    ]
    if not eligible:
        raise NoDistinguishableComboError(
            "no grid combo is distinguishable from passive AlwaysMarketRate"
        )
    # Negate span/sigma so max() prefers the smallest values on a mean_active tie.
    return max(
        eligible,
        key=lambda pm: (pm[1], -int(pm[0]["ema_span"]), -pm[0]["threshold_sigma"]),
    )[0]


def derive_cell_params(
    candles: list[FundingCandle],
    *,
    config: BacktestConfig,
    fill_model: FillRateModel,
) -> DerivedCell:
    """Derive the deployed MeanReversion params for one cell from its candles.

    Deterministic given `candles`. ratio_sigma per ema_span is computed from
    EDA on the train portion only (no look-ahead); each grid combo is then
    evaluated fixed over all rolling WFO windows.

    Selection uses mean_active + distinguishability only; it does NOT run the
    bootstrap not_worse check. The CI gate (pytest -m gate) is the downstream
    guard, so `--write` can succeed yet CI fail if a winner's CI low dips < 0.
    """
    if not candles:
        raise ValueError("derive_cell_params: empty candles")
    symbol = candles[0].symbol
    period_agg = candles[0].period_agg
    timeframe = candles[0].timeframe

    train_end_mts = compute_train_end_mts(candles)
    train = [c for c in candles if c.mts <= train_end_mts]

    eda: dict[str, Any] = {
        "close_over_ema_sigma_24": close_over_ema_sigma(train, ema_span=24),
        "close_over_ema_sigma_168": close_over_ema_sigma(train, ema_span=168),
    }

    grid = MeanReversionStrategy.param_grid_for_cell(
        symbol=symbol, period_agg=period_agg, eda=eda
    )
    windows = compute_wfo_windows(candles)
    if not windows:
        raise ValueError(
            f"derive_cell_params: no WFO windows for {symbol}_{period_agg}"
        )

    def _make_strategy(p: dict[str, Any]) -> MeanReversionStrategy:
        return MeanReversionStrategy(
            ema_span=int(p["ema_span"]),
            threshold_sigma=p["threshold_sigma"],
            ratio_sigma=p["ratio_sigma"],
        )

    scored: list[ScoredCombo] = []
    for params in grid:
        strat_out, base_out = evaluate_oos_windows(
            candles,
            windows,
            make_strategy=functools.partial(_make_strategy, params),
            config=config,
            fill_model=fill_model,
        )
        active = active_return_summary(strat_out, base_out)
        scored.append((
            params,
            active.mean_active,
            active.information_ratio,
            active.pct_months_outperform,
        ))

    winner = select_winner(scored)
    # select_winner returns the same params dict object stored in scored, so
    # identity (`is`) recovers that combo's stats without re-matching on value.
    winner_stats = next(
        (ma, ir, pct) for (p, ma, ir, pct) in scored if p is winner
    )
    return DerivedCell(
        symbol=symbol,
        period_agg=period_agg,
        timeframe=timeframe,
        ema_span=int(winner["ema_span"]),
        threshold_sigma=winner["threshold_sigma"],
        ratio_sigma=winner["ratio_sigma"],
        mean_active=winner_stats[0],
        information_ratio=winner_stats[1],
        pct_outperform=winner_stats[2],
        n_windows=len(windows),
    )
