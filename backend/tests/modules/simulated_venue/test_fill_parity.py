"""The venue's tick fills in the same hour `BookReplayLearner` says, per spread bucket."""
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.lending.tracking.book_replay import BookReplayLearner
from bfx_funding_bot.modules.simulated_venue._internal.decide import (
    catch_up,
    decide_submit,
    fund_wallet,
)
from bfx_funding_bot.modules.simulated_venue._internal.state import VenueState, apply
from bfx_funding_bot.modules.simulated_venue.contracts import BookSnapshot, PublicTrade
from tests.modules.lending.tracking.test_book_replay_golden import golden_inputs
from tests.modules.simulated_venue.helpers import HOUR, config

D = Decimal
HORIZON_H = 24
BUCKETS = [-500, -100, 0, 100, 300]


def _venue_fill_ms(snap: BookSnapshot, candles: list, ref: Decimal, bps: int) -> int | None:
    cfg = config()
    state = VenueState()
    t = snap.captured_at_ms
    apply(state, fund_wallet("UST", D("1000"), t)[0])
    rate = ref * (D(1) + D(bps) / D(10_000))
    placed = decide_submit(
        state, cfg, now_ms=t, symbol="fUST", amount=D("150"), rate=rate, period=2, book=snap,
    )
    assert isinstance(placed, list)
    for event in placed:
        apply(state, event)
    # A candle's volume traded "somewhere inside the hour": the venue sees it as one
    # public trade at the hour's end, which is exactly where book_replay stamps the fill.
    prints = [PublicTrade(c.mts + HOUR, c.volume or D(0), ref, 2) for c in candles
              if c.mts >= t and c.mts + HOUR <= t + HORIZON_H * HOUR]
    for event in catch_up(state, cfg, now_ms=t + HORIZON_H * HOUR, trades={"fUST": prints}):
        apply(state, event)
    (offer,) = state.offers.values()
    return offer.terminal_mts if offer.status == "EXECUTED" else None


def test_same_snapshot_and_candles_fill_in_the_same_hour_per_bucket() -> None:
    snapshots, candles = golden_inputs()
    by_mts = {c.mts: c for c in candles}
    on_the_hour = [s for s in snapshots if s.captured_at_ms % HOUR == 0][:12]
    assert len(on_the_hour) >= 6
    compared = filled = 0
    for snap in on_the_hour:
        ref_candle = by_mts.get(snap.captured_at_ms - HOUR)
        if ref_candle is None or ref_candle.close is None:
            continue
        for bps in BUCKETS:
            learner = BookReplayLearner(bucket_grid=[bps], horizons=[HORIZON_H])
            stats = learner.learn([snap], candles)
            if not stats:
                continue
            (stat,) = stats
            venue = _venue_fill_ms(snap, candles, ref_candle.close, bps)
            compared += 1
            if stat.fill_prob == 1:
                filled += 1
                assert venue is not None
                assert venue - snap.captured_at_ms == stat.ttf_p50_ms
            else:
                assert venue is None
    assert compared >= 20 and 0 < filled < compared  # both fills and misses are exercised
