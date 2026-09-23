"""Book-replay fill learner: queue position + traded volume decide the fill."""
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.book_replay import (
    BookReplayLearner,
    queue_ahead,
)
from bfx_funding_bot.modules.marketfeed.book_period_coverage import BookAskSnapshot

_H = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z


def _candle(mts: int, close: str, volume: str) -> FundingCandle:
    return FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
                         close=Decimal(close), volume=Decimal(volume))


def _snap(t: int, asks: list[tuple[str, int, str]]) -> BookAskSnapshot:
    return BookAskSnapshot(symbol="fUST", captured_at_ms=t,
                           asks=tuple((Decimal(r), p, Decimal(a)) for r, p, a in asks))


def _hours(n: int, close: str = "0.0002", volume: str = "1000") -> list[FundingCandle]:
    return [_candle(_T0 + i * _H, close, volume) for i in range(n)]


def test_queue_ahead_counts_same_period_at_or_below_offer() -> None:
    snap = _snap(_T0, [("0.00019", 2, "100"), ("0.0002", 2, "200"),
                       ("0.00021", 2, "300"), ("0.0002", 30, "999")])
    assert queue_ahead(snap, period_days=2, offer_rate=Decimal("0.0002")) == Decimal("300")
    assert queue_ahead(snap, period_days=2, offer_rate=Decimal("0.00018")) == Decimal("0")
    assert queue_ahead(snap, period_days=30, offer_rate=Decimal("0.0002")) == Decimal("999")


def test_fill_requires_volume_to_consume_the_queue_ahead() -> None:
    # Snapshot at 02:37; ref = close of 01:00 (last completed hour) = 0.0002.
    # At +0 bps the offer sits behind 2500 of resting asks; hourly volume 1000.
    t = _T0 + 2 * _H + 37 * 60_000
    snap = _snap(t, [("0.0002", 2, "2500")])
    candles = _hours(8)
    learner = BookReplayLearner(period_days=2, offer_amount=Decimal("150"),
                                bucket_grid=[-100, 0], horizons=[1, 4])
    stats = {(s.horizon_h, s.spread_bucket_bps): s for s in learner.learn([snap], candles)}
    # 1h horizon from 02:37 ends 03:37; the 03:00 candle runs to 04:00 and straddles it,
    # so the horizon is unresolvable: no sample, not a miss (the old code counted the
    # 04:00 fill as "within 1h" and reported an 83-minute time-to-fill).
    assert not any(h == 1 for h, _ in stats)
    # -100 bps undercuts the whole queue: needs 150 of volume -> 03:00 candle, done by 04:00.
    assert stats[(4, -100)].fill_prob == Decimal("1")
    assert stats[(4, -100)].ttf_p50_ms == (_T0 + 4 * _H) - t
    # +0 bps: needs 2650 -> 03:00+04:00+05:00 candles (3000), done by 06:00 <= 06:37.
    assert stats[(4, 0)].fill_prob == Decimal("1")
    assert stats[(4, 0)].n_samples == 1


def test_horizon_counts_only_candles_wholly_inside_the_window() -> None:
    # Snapshot on the hour: the 02:00 candle starts at t and ends at 03:00 = t+1h -> in.
    t = _T0 + 2 * _H
    candles = _hours(8)
    learner = BookReplayLearner(bucket_grid=[-100], horizons=[1])
    (s,) = learner.learn([_snap(t, [("0.0002", 2, "10")])], candles)
    assert (s.horizon_h, s.fill_prob, s.ttf_p50_ms) == (1, Decimal("1"), _H)
    # One minute later the same candle straddles t+1h -> no 1h sample at all.
    assert learner.learn([_snap(t + 60_000, [("0.0002", 2, "10")])], candles) == []


def test_snapshot_without_completed_reference_hour_is_skipped() -> None:
    t = _T0 + 30 * 60_000  # 00:30 -> reference hour would be 23:00 the day before: absent
    learner = BookReplayLearner(bucket_grid=[0], horizons=[1])
    assert learner.learn([_snap(t, [("0.0002", 2, "10")])], _hours(4)) == []


def test_missing_volume_counts_as_zero_and_never_fills() -> None:
    t = _T0 + 2 * _H
    candles = [_candle(_T0 + i * _H, "0.0002", "0") for i in range(6)]
    candles[3] = FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                               mts=_T0 + 3 * _H, close=Decimal("0.0002"), volume=None)
    learner = BookReplayLearner(bucket_grid=[-500], horizons=[4])
    (s,) = learner.learn([_snap(t, [])], candles)
    assert s.fill_prob == Decimal("0") and s.ttf_p50_ms is None


def test_period_agg_key_matches_period_and_validation() -> None:
    assert BookReplayLearner(period_days=30).period_agg == "p30"
    with pytest.raises(ValueError):
        BookReplayLearner(period_days=1)
    with pytest.raises(ValueError):
        BookReplayLearner(offer_amount=Decimal("0"))
