"""Unit tests for the pure helpers in scripts._g3_loaders.

The DB-backed build_verdict_from_neon path is covered by
test_g3_loaders_integration.py (marked integration, skipped by the commit gate).
"""
from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.live_validation.live_attribution import MarketRatePoint
from scripts._g3_loaders import _candles_to_market_rate_points


def _candle(mts: int, close: Decimal | None) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts, close=close
    )


def test_candles_to_market_rate_points_maps_close_to_rate():
    candles = [_candle(1000, Decimal("0.0002")), _candle(2000, Decimal("0.0003"))]
    assert _candles_to_market_rate_points(candles) == [
        MarketRatePoint(mts=1000, rate=Decimal("0.0002")),
        MarketRatePoint(mts=2000, rate=Decimal("0.0003")),
    ]


def test_candles_to_market_rate_points_skips_none_close():
    candles = [_candle(1000, None), _candle(2000, Decimal("0.0003"))]
    assert _candles_to_market_rate_points(candles) == [
        MarketRatePoint(mts=2000, rate=Decimal("0.0003"))
    ]


def test_candles_to_market_rate_points_empty():
    assert _candles_to_market_rate_points([]) == []
