from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import _apply_friction
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel


@dataclass
class _Row:
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None


def _candle() -> FundingCandle:
    return FundingCandle(symbol="fUSD", timeframe="1h", period_agg="p2", mts=0,
                         open=Decimal("0.0003"), close=Decimal("0.0003"),
                         high=Decimal("0.0003"), low=Decimal("0.0003"), volume=Decimal("0"))


def _decision() -> LendDecision:
    # offer +50 bps above ref (0.0003 * 1.005)
    return LendDecision(mts=0, rate=Decimal("0.0003") * Decimal("1.005"), period_days=2)


def _model() -> FillRateModel:
    return FillRateModel.from_rows([
        _Row("p2", 4, 0, 1.0, 100, 1000),
        _Row("p2", 4, 100, 0.5, 100, 5000),
    ])


def test_empirical_uses_learned_fill_prob():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    _, fill_prob = _apply_friction(_decision(), _candle(), cfg, _model())
    # +50 bps interpolates 1.0↔0.5 → 0.75 (NOT the linear-model value)
    assert fill_prob == Decimal("0.75")


def test_empirical_falls_back_to_linear_when_model_none():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    _, fp_fallback = _apply_friction(_decision(), _candle(), cfg, None)
    # linear: spread_pct = 0.005 → 1 - 5*0.005 = 0.975
    assert fp_fallback == Decimal("0.975")


def test_linear_mode_ignores_model():
    cfg = BacktestConfig(fill_model="linear")
    _, fp = _apply_friction(_decision(), _candle(), cfg, _model())
    assert fp == Decimal("0.975")  # linear, model ignored
