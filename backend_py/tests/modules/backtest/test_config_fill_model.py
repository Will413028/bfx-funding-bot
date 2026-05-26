import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig


def test_default_fill_model_is_empirical():
    cfg = BacktestConfig()
    assert cfg.fill_model == "empirical"
    assert cfg.fill_horizon_h == 4


def test_explicit_linear_allowed():
    cfg = BacktestConfig(fill_model="linear")
    assert cfg.fill_model == "linear"


def test_invalid_fill_horizon_rejected():
    with pytest.raises(ValueError):
        BacktestConfig(fill_horizon_h=0)
