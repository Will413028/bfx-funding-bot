"""Per-series empirical fill models: each tenor is scored by its own book evidence."""
from dataclasses import dataclass
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import (
    EMPIRICAL_SOURCES,
    BacktestIncomplete,
    run_backtest,
)
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

_HOUR = 3_600_000
_T0 = 1_704_067_200_000
_EMPIRICAL = BacktestConfig(fill_model="empirical", fill_horizon_h=4)


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


def _series(period_agg: str, close: str, n: int = 240) -> list[FundingCandle]:
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg=period_agg,
                          mts=_T0 + i * _HOUR, close=Decimal(close)) for i in range(n)]


def _model(period_agg: str, *, at_par: float, source: str = "book", symbol: str = "fUST") -> FillRateModel:
    h = f"{source}-{period_agg}-v1"
    artifact = FillModelArtifact(
        symbol=symbol, period_agg=period_agg, horizon_h=4, source=source,
        model_version="book-replay-v1", schema_version=1, artifact_hash=h,
        training_start_ms=0, training_end_ms=1, cutoff_ms=1, sample_count=100,
        confidence_min_samples=30,
    )
    return FillRateModel.from_rows(
        [_Row(source, symbol, period_agg, 4, -100, 1.0, 100, 1000, h),
         _Row(source, symbol, period_agg, 4, 0, at_par, 100, 1000, h),
         _Row(source, symbol, period_agg, 4, 100, 0.0, 100, None, h)],
        artifact=artifact,
    )


def test_book_source_is_an_accepted_empirical_source() -> None:
    assert {"candle", "book"} == EMPIRICAL_SOURCES
    p2 = _series("p2", "0.0002")
    r = run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                     fill_model=_model("p2", at_par=0.6))
    assert r.fill_rate == Decimal("0.6")


def test_each_tenor_is_scored_by_its_own_model() -> None:
    p2, p30 = _series("p2", "0.0002"), _series("p30", "0.0004")
    series = {"p2": p2, "p30": p30}
    models = {"p2": _model("p2", at_par=0.6), "p30": _model("p30", at_par=0.3)}
    two = run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                       market_series_by_agg=series, fill_models_by_agg=models)
    thirty = run_backtest(p30, AlwaysMarketRateStrategy(period_days=30), _EMPIRICAL,
                          market_series_by_agg=series, fill_models_by_agg=models)
    assert two.fill_rate == Decimal("0.6") and thirty.fill_rate == Decimal("0.3")
    assert two.fill_models_by_series == {"p2": "book-p2-v1", "p30": "book-p30-v1"}


def test_series_without_a_model_is_incomplete_not_linear() -> None:
    p2, a30 = _series("p2", "0.0002"), _series("a30", "0.0003")
    series = {"p2": p2, "a30": a30}
    with pytest.raises(BacktestIncomplete, match="fill_model_missing"):
        # 14-day offers price off a30, which has no book model.
        run_backtest(a30, AlwaysMarketRateStrategy(period_days=14), _EMPIRICAL,
                     market_series_by_agg=series, fill_models_by_agg={"p2": _model("p2", at_par=0.6)})


def test_model_scoped_to_another_series_is_rejected_up_front() -> None:
    p2, p30 = _series("p2", "0.0002"), _series("p30", "0.0004")
    with pytest.raises(BacktestIncomplete, match="scope_mismatch"):
        run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                     market_series_by_agg={"p2": p2, "p30": p30},
                     fill_models_by_agg={"p2": _model("p30", at_par=0.3)})


def test_unknown_source_is_still_rejected() -> None:
    p2 = _series("p2", "0.0002")
    with pytest.raises(BacktestIncomplete, match="scope_mismatch"):
        run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                     fill_model=_model("p2", at_par=0.6, source="own_fill"))


def test_argument_exclusivity() -> None:
    p2 = _series("p2", "0.0002")
    with pytest.raises(ValueError, match="not both"):
        run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                     market_series_by_agg={"p2": p2},
                     fill_model=_model("p2", at_par=0.6), fill_models_by_agg={"p2": _model("p2", at_par=0.6)})
    with pytest.raises(ValueError, match="requires market_series_by_agg"):
        run_backtest(p2, AlwaysMarketRateStrategy(period_days=2), _EMPIRICAL,
                     fill_models_by_agg={"p2": _model("p2", at_par=0.6)})
