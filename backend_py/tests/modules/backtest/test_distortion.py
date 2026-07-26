from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.distortion import perturb_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="a30", mts=mts,
        open=Decimal("0.0001"), close=Decimal(close),
        high=Decimal("0.0003"), low=Decimal("0.00005"), volume=Decimal("100"),
    )


def test_zero_distortion_rate_returns_the_series_untouched() -> None:
    """The no-distortion arm must be identical, or the comparison is meaningless."""
    candles = [_candle(1000 + i, "0.0002") for i in range(10)]

    out = perturb_candles(candles, distortion_rate=0.0, pct_samples=[-35.0], seed=1)

    assert out == candles


def test_full_distortion_applies_the_observed_percentage_to_close() -> None:
    """pct is (final - live)/live, so recovering live divides by (1 + pct/100).

    A final of 0.0002 that was observed 33.3% lower means live saw 0.00015.
    """
    candles = [_candle(1000, "0.0002")]

    out = perturb_candles(candles, distortion_rate=1.0, pct_samples=[33.3], seed=1)

    assert out[0].close == pytest.approx(Decimal("0.0002") / Decimal("1.333"))


def test_distortion_touches_close_only() -> None:
    """Only close reaches a strategy decision; perturbing OHLV would confound."""
    candles = [_candle(1000, "0.0002")]

    out = perturb_candles(candles, distortion_rate=1.0, pct_samples=[-20.0], seed=1)

    assert out[0].open == candles[0].open
    assert out[0].high == candles[0].high
    assert out[0].low == candles[0].low
    assert out[0].volume == candles[0].volume
    assert out[0].mts == candles[0].mts


def test_same_seed_reproduces_the_same_series() -> None:
    """Monte Carlo runs must be reproducible or the result cannot be audited."""
    candles = [_candle(1000 + i, "0.0002") for i in range(50)]
    kwargs = {"distortion_rate": 0.174, "pct_samples": [-35.3, 13.7, -1.0], "seed": 42}

    assert perturb_candles(candles, **kwargs) == perturb_candles(candles, **kwargs)


def test_distortion_rate_governs_how_many_candles_move() -> None:
    """At 17.4% the count should land near the observed 23-of-132 rate."""
    candles = [_candle(1000 + i, "0.0002") for i in range(2000)]

    out = perturb_candles(
        candles, distortion_rate=0.174, pct_samples=[-35.3], seed=7
    )

    moved = sum(1 for a, b in zip(candles, out, strict=True) if a.close != b.close)
    assert 0.15 * len(candles) < moved < 0.20 * len(candles)
