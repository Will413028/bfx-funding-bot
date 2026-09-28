from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion_frr_floor import (
    MeanReversionFrrFloorStrategy,
)

FRR = Decimal("0.0002")  # per-day parking rate the lookup returns


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def _floor(frr_at=lambda mts: FRR) -> MeanReversionFrrFloorStrategy:
    return MeanReversionFrrFloorStrategy(
        ema_span=5, threshold_sigma=Decimal("2.0"), ratio_sigma=Decimal("0.10"),
        frr_at=frr_at,
    )


def test_name_includes_params_and_frr_floor() -> None:
    s = _floor()
    assert s.name == "mean_reversion_frr_floor_ema5_sigma2.0"


def test_non_skip_lends_at_close_like_plain_mr() -> None:
    # close at EMA -> deviation 0 >= lower band -> plain MR path, rate=close
    s = _floor()
    s.observe(_c(0, "0.0010"))
    s.observe(_c(1, "0.0010"))
    cand = _c(2, "0.0010")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.rate == Decimal("0.0010")
    assert d.period_days == 2


def test_skip_branch_parks_at_frr_instead_of_none() -> None:
    # Same setup as MR's skip test: deviation ~-0.222 < lower band -0.20.
    # Plain MR returns None (idle); the floor variant parks at FRR.
    plain = MeanReversionStrategy(
        ema_span=5, threshold_sigma=Decimal("2.0"), ratio_sigma=Decimal("0.10"),
    )
    floor = _floor()
    for c in (_c(0, "0.0010"), _c(1, "0.0010")):
        plain.observe(c)
        floor.observe(c)
    cand = _c(2, "0.0007")
    plain.observe(cand)
    floor.observe(cand)
    assert plain.decide(cand) is None
    d = floor.decide(cand)
    assert d is not None
    assert d.rate == FRR
    assert d.period_days == 2
    assert d.mts == cand.mts


def test_skip_branch_returns_none_when_frr_unavailable() -> None:
    floor = _floor(frr_at=lambda mts: None)
    for c in (_c(0, "0.0010"), _c(1, "0.0010")):
        floor.observe(c)
    cand = _c(2, "0.0007")
    floor.observe(cand)
    assert floor.decide(cand) is None


def test_decide_returns_none_before_ema_initialized() -> None:
    assert _floor().decide(_c(0, "0.0001")) is None


def test_decide_returns_none_when_close_is_none() -> None:
    s = _floor()
    s.observe(_c(0, "0.0001"))
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(cand)
    assert s.decide(cand) is None


def test_last_deviation_tracked_on_skip_branch() -> None:
    floor = _floor()
    for c in (_c(0, "0.0010"), _c(1, "0.0010")):
        floor.observe(c)
    cand = _c(2, "0.0007")
    floor.observe(cand)
    floor.decide(cand)
    assert floor.last_deviation is not None
    assert floor.last_deviation < Decimal("-0.20")
