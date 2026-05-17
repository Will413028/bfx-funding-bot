from decimal import Decimal

from bfx_funding_bot.modules.backtest.eda import (
    autocorrelation_at_lags,
    close_over_ema_sigma,
    close_rate_percentiles,
    mean_rate_by_weekday,
    per_quarter_regime_drift,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_close_rate_percentiles_returns_p25_p50_p75_p90() -> None:
    candles = [_candle(i, str(0.0001 * (i + 1))) for i in range(100)]
    pct = close_rate_percentiles(candles)
    assert "P25" in pct and "P50" in pct and "P75" in pct and "P90" in pct
    # values ascending
    assert pct["P25"] < pct["P50"] < pct["P75"] < pct["P90"]


def test_mean_rate_by_weekday_returns_seven_entries() -> None:
    # 14 days of hourly candles starting Mon 2024-01-01 00:00 UTC
    start = 1704067200000
    candles = [_candle(start + i * 3_600_000, "0.0001") for i in range(14 * 24)]
    by_wd = mean_rate_by_weekday(candles)
    assert set(by_wd.keys()) == {0, 1, 2, 3, 4, 5, 6}
    # all means equal (constant rate)
    means = list(by_wd.values())
    assert all(abs(m - means[0]) < Decimal("0.0001") for m in means)


def test_close_over_ema_sigma_with_constant_returns_zero() -> None:
    candles = [_candle(i * 3_600_000, "0.0001") for i in range(200)]
    sigma = close_over_ema_sigma(candles, ema_span=24)
    assert sigma < Decimal("0.001")  # essentially zero


def test_autocorrelation_at_lags_constant_series_returns_none_or_one() -> None:
    candles = [_candle(i * 3_600_000, "0.0001") for i in range(200)]
    # constant series: ACF undefined (var=0); we return None for undefined
    acf = autocorrelation_at_lags(candles, lags=[1, 24])
    assert acf[1] is None or acf[1] == Decimal("1")


def test_per_quarter_regime_drift_returns_max_min_ratio() -> None:
    # 8 quarters with mean rates: 0.0001 (Q1-Q4), 0.0002 (Q5-Q8)
    # max/min relative drift = (0.0002 - 0.0001) / 0.0001 = 1.0
    candles = []
    quarter_seconds = 90 * 24 * 3600
    for q in range(8):
        rate = "0.0001" if q < 4 else "0.0002"
        for h in range(24):
            ts = (q * quarter_seconds + h * 3600) * 1000
            candles.append(_candle(ts, rate))
    drift = per_quarter_regime_drift(candles)
    assert drift > Decimal("0.99")  # close to 1.0
