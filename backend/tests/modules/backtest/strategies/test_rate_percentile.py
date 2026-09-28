from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.strategies.rate_percentile import (
    RatePercentileStrategy,
)


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_name_includes_params() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=168)
    assert s.name == "rate_percentile_p50_n168"


def test_decide_returns_none_during_warmup() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=10)
    for i in range(9):
        s.observe(_c(i, "0.0001"))
    d = s.decide(_c(9, "0.0001"))
    assert d is None


def test_decide_emits_when_close_above_threshold() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=4)
    for c in [_c(0, "0.0001"), _c(1, "0.0002"), _c(2, "0.0003"), _c(3, "0.0004")]:
        s.observe(c)
    cand = _c(4, "0.0005")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.rate == Decimal("0.0005")
    assert d.period_days == 2


def test_decide_returns_none_when_close_below_threshold() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=4)
    for c in [_c(0, "0.0001"), _c(1, "0.0002"), _c(2, "0.0003"), _c(3, "0.0004")]:
        s.observe(c)
    cand = _c(4, "0.0001")
    s.observe(cand)
    d = s.decide(cand)
    assert d is None


def test_decide_returns_none_when_close_is_none() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=2)
    s.observe(_c(0, "0.0001"))
    s.observe(_c(1, "0.0002"))
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=2,
        open=None, close=None, high=None, low=None, volume=None,
    )
    assert s.decide(cand) is None


def test_param_grid_for_cell_with_acf_pass_returns_six_variants() -> None:
    grid = RatePercentileStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"acf_168h_pass": True},
    )
    assert len(grid) == 6
    pcts = {p["percentile"] for p in grid}
    looks = {p["lookback_hours"] for p in grid}
    assert pcts == {25, 50, 75}
    assert looks == {168, 720}


def test_param_grid_for_cell_with_acf_fail_drops_720_lookback() -> None:
    """Per spec: ACF lag 168h < 0.3 -> drop 720 variant (only 168 remains)."""
    grid = RatePercentileStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"acf_168h_pass": False},
    )
    assert len(grid) == 3
    looks = {p["lookback_hours"] for p in grid}
    assert looks == {168}


def _rp(percentile: int = 75, lookback: int = 3) -> RatePercentileStrategy:
    return RatePercentileStrategy(percentile=percentile, lookback_hours=lookback)


def test_window_filled_false_until_lookback_reached() -> None:
    rp = _rp(lookback=3)
    assert rp.window_filled is False
    rp.observe(_c(0, "0.0001"))
    rp.observe(_c(1, "0.0002"))
    assert rp.window_filled is False
    rp.observe(_c(2, "0.0003"))
    assert rp.window_filled is True


def test_last_threshold_none_during_warmup() -> None:
    rp = _rp(lookback=3)
    rp.observe(_c(0, "0.0001"))
    cand = _c(1, "0.0002")
    rp.observe(cand)
    rp.decide(cand)  # window not full -> decide returns early, no threshold cached
    assert rp.last_threshold is None


def test_last_threshold_caches_decide_value() -> None:
    import numpy as np

    rp = _rp(percentile=50, lookback=3)
    for i, v in enumerate(["0.0001", "0.0003", "0.0002"]):
        rp.observe(_c(i, v))
    cand = _c(3, "0.0002")
    rp.observe(cand)  # window now [0.0003, 0.0002, 0.0002] (maxlen=3)
    rp.decide(cand)
    assert rp.last_threshold is not None
    win = [0.0003, 0.0002, 0.0002]
    expected = Decimal(str(float(np.percentile(win, 50))))
    assert rp.last_threshold == expected


def test_window_values_returns_current_window_snapshot() -> None:
    rp = _rp(lookback=3)
    rp.observe(_c(0, "0.0001"))
    rp.observe(_c(1, "0.0002"))
    assert rp.window_values == (Decimal("0.0001"), Decimal("0.0002"))
    rp.observe(_c(2, "0.0003"))
    rp.observe(_c(3, "0.0004"))
    assert rp.window_values == (Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0004"))
