from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.fill_rate import (
    FillRateLearner,
)

_H = 3_600_000  # ms per hour


def _candle(mts: int, close: float, high: float) -> FundingCandle:
    return FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(str(close)), close=Decimal(str(close)),
        high=Decimal(str(high)), low=Decimal(str(close)), volume=Decimal("0"),
    )


def test_offer_below_market_always_fills_immediately():
    # Flat path: close=high=0.0003 every hour. An offer at -50 bps (below ref)
    # is < ref, so the very next candle's high (== ref) crosses it → fill at H+1.
    candles = [_candle(i * _H, 0.0003, 0.0003) for i in range(6)]
    learner = FillRateLearner(bucket_grid=[-50, 0, 100], horizons=[1])
    stats = {(s.spread_bucket_bps): s for s in learner.learn(candles)}
    # -50 bps offer = 0.0003 * 0.995 < 0.0003 high → fills
    assert stats[-50].fill_prob == Decimal("1")
    # +100 bps offer = 0.0003 * 1.01 > 0.0003 high → never fills on a flat path
    assert stats[100].fill_prob == Decimal("0")
    assert stats[100].ttf_p50_ms is None


def test_time_to_fill_is_candle_granular():
    # Rising path: high jumps to a level that a +100bps offer crosses only at hour 2.
    # ref at t0 = 0.0003; +100bps offer = 0.000303.
    # highs: h0=0.0003, h1=0.0003, h2=0.00031 (crosses), within horizon 4h.
    candles = [
        _candle(0 * _H, 0.0003, 0.0003),
        _candle(1 * _H, 0.0003, 0.0003),
        _candle(2 * _H, 0.0003, 0.00031),
        _candle(3 * _H, 0.0003, 0.00031),
    ]
    learner = FillRateLearner(bucket_grid=[100], horizons=[4])
    stats = {s.spread_bucket_bps: s for s in learner.learn(candles)}
    s = stats[100]
    # From t0, first crossing is candle at 2h → ttf = 2h. (t1,t2,t3 also evaluated
    # but only t0/t1 have a crossing within their windows; assert t0's ttf present.)
    assert s.fill_prob > Decimal("0")
    assert s.ttf_p50_ms is not None and s.ttf_p50_ms <= 2 * _H


def test_skips_none_or_nonpositive_reference():
    candles = [
        FundingCandle(symbol="fUSD", timeframe="1h", period_agg="p2", mts=0,
                      open=None, close=None, high=None, low=None, volume=None),
        _candle(1 * _H, 0.0003, 0.0003),
        _candle(2 * _H, 0.0003, 0.0003),
    ]
    learner = FillRateLearner(bucket_grid=[0], horizons=[1])
    stats = learner.learn(candles)
    # Only the candle at 1h has a valid ref AND a following candle within 1h? No —
    # candle at 1h's window (1h, 2h] includes the 2h candle → 1 sample. The None
    # candle contributes nothing.
    assert all(s.n_samples >= 1 for s in stats)


def test_empty_candles_returns_empty():
    assert FillRateLearner().learn([]) == []
