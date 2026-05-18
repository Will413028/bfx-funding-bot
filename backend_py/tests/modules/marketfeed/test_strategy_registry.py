from __future__ import annotations

import pytest

from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)


def _cell_mr() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_alpha": 0.02},
        "reference_amount_usdt": 150.0,
    })


def _cell_rp() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 168},
        "reference_amount_usdt": 150.0,
    })


def test_build_strategy_mean_reversion():
    s = build_strategy(_cell_mr())
    assert isinstance(s, MeanReversionStrategy)


def test_build_strategy_rate_percentile():
    s = build_strategy(_cell_rp())
    assert isinstance(s, RatePercentileStrategy)


def test_registry_put_get():
    cell = _cell_mr()
    reg = StrategyRegistry()
    s = build_strategy(cell)
    reg.put(cell, s)
    assert reg.get(cell) is s


def test_registry_missing_raises():
    reg = StrategyRegistry()
    with pytest.raises(KeyError):
        reg.get(_cell_mr())
