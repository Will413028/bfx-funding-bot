from dataclasses import dataclass
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import _apply_friction, run_backtest
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import AlwaysMarketRateStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel


@dataclass
class _Row:
    source: str
    symbol: str
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None
    artifact_hash: str


def _candle() -> FundingCandle:
    return FundingCandle(symbol="fUSD", timeframe="1h", period_agg="p2", mts=0,
                         open=Decimal("0.0003"), close=Decimal("0.0003"),
                         high=Decimal("0.0003"), low=Decimal("0.0003"), volume=Decimal("0"))


def _decision() -> LendDecision:
    # offer +50 bps above ref (0.0003 * 1.005)
    return LendDecision(mts=0, rate=Decimal("0.0003") * Decimal("1.005"), period_days=2)


def _model() -> FillRateModel:
    artifact = FillModelArtifact(
        symbol="fUSD", period_agg="p2", horizon_h=4, source="candle",
        model_version="g13-candle-v1", schema_version=1, artifact_hash="artifact-v1",
        training_start_ms=0, training_end_ms=10_000, cutoff_ms=10_000,
        sample_count=200, confidence_min_samples=30,
    )
    return FillRateModel.from_rows([
        _Row("candle", "fUSD", "p2", 4, 0, 1.0, 100, 1000, "artifact-v1"),
        _Row("candle", "fUSD", "p2", 4, 100, 0.5, 100, 5000, "artifact-v1"),
    ], artifact=artifact)


def test_empirical_uses_learned_fill_prob():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    _, fill_prob = _apply_friction(_decision(), _candle(), cfg, _model())
    # +50 bps interpolates 1.0↔0.5 → 0.75 (NOT the linear-model value)
    assert fill_prob == Decimal("0.75")


def test_empirical_backtest_does_not_use_linear_when_model_is_missing():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    with pytest.raises(Exception, match="fill_model_missing") as exc_info:
        run_backtest([_candle()], AlwaysMarketRateStrategy(period_days=2), cfg, fill_model=None)
    assert type(exc_info.value).__name__ == "BacktestIncomplete"


def test_linear_baseline_mode_ignores_model():
    cfg = BacktestConfig(fill_model="linear-baseline")
    _, fp = _apply_friction(_decision(), _candle(), cfg, _model())
    assert fp == Decimal("0.975")  # linear, model ignored
