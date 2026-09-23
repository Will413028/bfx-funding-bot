from dataclasses import dataclass
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import (
    BacktestIncomplete,
    _apply_friction,
    run_backtest,
)
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import AlwaysMarketRateStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
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
    artifact_hash: str | None


def _candle(*, symbol: str = "fUSD", period_agg: str = "p2") -> FundingCandle:
    return FundingCandle(symbol=symbol, timeframe="1h", period_agg=period_agg, mts=0,
                         open=Decimal("0.0003"), close=Decimal("0.0003"),
                         high=Decimal("0.0003"), low=Decimal("0.0003"), volume=Decimal("0"))


def _decision() -> LendDecision:
    # offer +50 bps above ref (0.0003 * 1.005)
    return LendDecision(mts=0, rate=Decimal("0.0003") * Decimal("1.005"), period_days=2)


def _artifact(*, symbol: str = "fUSD", source: str = "candle") -> FillModelArtifact:
    return FillModelArtifact(
        symbol=symbol, period_agg="p2", horizon_h=4, source=source,
        model_version="g13-candle-v1", schema_version=1, artifact_hash="artifact-v1",
        training_start_ms=0, training_end_ms=10_000, cutoff_ms=10_000,
        sample_count=200, confidence_min_samples=30,
    )


def _model(*, symbol: str = "fUSD", source: str = "candle") -> FillRateModel:
    artifact = _artifact(symbol=symbol, source=source)
    return FillRateModel.from_rows([
        _Row(source, symbol, "p2", 4, 0, 1.0, 100, 1000, "artifact-v1"),
        _Row(source, symbol, "p2", 4, 100, 0.5, 100, 5000, "artifact-v1"),
    ], artifact=artifact)


class _NeverLendsStrategy(Strategy):
    @property
    def name(self) -> str:
        return "never_lends"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        return None


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


def test_empirical_backtest_rejects_empty_candles_without_model():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        run_backtest([], AlwaysMarketRateStrategy(period_days=2), cfg, fill_model=None)


def test_empirical_backtest_rejects_zero_decision_without_model():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        run_backtest([_candle()], _NeverLendsStrategy(), cfg, fill_model=None)


def test_empirical_backtest_rejects_artifact_symbol_scope_mismatch():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest(
            [_candle(symbol="fUST")],
            AlwaysMarketRateStrategy(period_days=2),
            cfg,
            fill_model=_model(),
        )


def test_empirical_backtest_rejects_unknown_artifact_source():
    # "candle" and "book" are the accepted evidence sources (engine.EMPIRICAL_SOURCES);
    # anything else is a scope mismatch, never a silent pass-through.
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest(
            [_candle()],
            AlwaysMarketRateStrategy(period_days=2),
            cfg,
            fill_model=_model(source="own_fill"),
        )


def test_empirical_backtest_rejects_artifact_only_model_on_empty_candles():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    model = FillRateModel.from_rows([], artifact=_artifact())

    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        run_backtest([], AlwaysMarketRateStrategy(period_days=2), cfg, fill_model=model)


def test_empirical_backtest_rejects_unversioned_model_on_zero_decision():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    model = FillRateModel.from_rows([
        _Row("candle", "fUSD", "p2", 4, 0, 1.0, 100, 1000, None),
    ], artifact=_artifact())

    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        run_backtest([_candle()], _NeverLendsStrategy(), cfg, fill_model=model)


def test_empirical_backtest_rejects_scope_mismatch_model_on_zero_decision():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)
    model = FillRateModel.from_rows([
        _Row("candle", "fUST", "p2", 4, 0, 1.0, 100, 1000, "artifact-v1"),
    ], artifact=_artifact())

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest([_candle()], _NeverLendsStrategy(), cfg, fill_model=model)


def test_empirical_backtest_rejects_period_scope_mismatch_on_zero_decision():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest(
            [_candle(period_agg="p30")],
            _NeverLendsStrategy(),
            cfg,
            fill_model=_model(),
        )


def test_empirical_backtest_rejects_horizon_scope_mismatch_on_zero_decision():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=6)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest([_candle()], _NeverLendsStrategy(), cfg, fill_model=_model())


def test_empirical_backtest_rejects_horizon_scope_mismatch_on_empty_candles():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=6)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest([], AlwaysMarketRateStrategy(period_days=2), cfg, fill_model=_model())


def test_empirical_backtest_preflights_period_before_record_window_decision():
    cfg = BacktestConfig(fill_model="empirical", fill_horizon_h=4)

    with pytest.raises(BacktestIncomplete, match="fill_model_scope_mismatch"):
        run_backtest(
            [_candle(period_agg="p30")],
            _NeverLendsStrategy(),
            cfg,
            record_start_mts=1_000,
            record_end_mts=1_000,
            fill_model=_model(),
        )


def test_linear_baseline_mode_ignores_model():
    cfg = BacktestConfig(fill_model="linear-baseline")
    _, fp = _apply_friction(_decision(), _candle(), cfg, _model())
    assert fp == Decimal("0.975")  # linear, model ignored
