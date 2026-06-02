from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def _ap(ema_span: int = 24, t1: str = "0.5", t2: str = "1.5",
        ratio_sigma: str = "0.05", p_mid: int = 7, p_long: int = 30) -> AdaptivePeriodStrategy:
    return AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=Decimal(ratio_sigma),
        t1=Decimal(t1), t2=Decimal(t2), p_mid=p_mid, p_long=p_long,
    )


def test_name_includes_params() -> None:
    assert _ap().name == "adaptive_period_ema24_t0.5_1.5"


def test_ema_current_none_before_first_observe() -> None:
    assert _ap().ema_current is None


def test_observe_seeds_then_tracks_ema() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    assert s.ema_current == Decimal("0.0003")  # first observe seeds
    s.observe(_c(3600_000, "0.0005"))
    alpha = Decimal(2) / Decimal(24 + 1)
    expected = alpha * Decimal("0.0005") + (Decimal("1") - alpha) * Decimal("0.0003")
    assert s.ema_current == expected


def test_observe_skips_none_close_and_does_not_count_sample() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    none_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(none_candle)
    assert s.samples == 1
    assert s.ema_current == Decimal("0.0003")


def test_window_filled_after_ema_span_samples() -> None:
    s = _ap(ema_span=3)
    assert not s.window_filled
    for i in range(3):
        s.observe(_c(i, "0.0003"))
    assert s.window_filled
