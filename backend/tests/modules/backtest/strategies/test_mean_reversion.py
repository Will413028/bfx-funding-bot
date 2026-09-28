from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion import (
    MeanReversionStrategy,
)


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_name_includes_params() -> None:
    s = MeanReversionStrategy(
        ema_span=24, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.05"),
    )
    assert s.name == "mean_reversion_ema24_sigma1.0"


def test_decide_returns_none_before_ema_initialized() -> None:
    s = MeanReversionStrategy(
        ema_span=24, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.05"),
    )
    d = s.decide(_c(0, "0.0001"))
    assert d is None


def test_decide_emits_when_close_above_lower_band() -> None:
    s = MeanReversionStrategy(
        ema_span=2, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.05"),
    )
    s.observe(_c(0, "0.0001"))
    s.observe(_c(1, "0.0001"))
    cand = _c(2, "0.0001")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.period_days == 2


def test_decide_returns_none_when_close_below_lower_band() -> None:
    # ema_span=5 -> alpha=1/3; after two observations of 0.0010 EMA=0.0010,
    # then observe(0.0007): EMA = 1/3*0.0007 + 2/3*0.0010 = 0.0009
    # deviation = (0.0007 - 0.0009)/0.0009 ~ -0.222
    # lower band = -2.0 * 0.10 = -0.20; deviation < -0.20 -> None
    s = MeanReversionStrategy(
        ema_span=5, threshold_sigma=Decimal("2.0"), ratio_sigma=Decimal("0.10"),
    )
    s.observe(_c(0, "0.0010"))
    s.observe(_c(1, "0.0010"))
    cand = _c(2, "0.0007")
    s.observe(cand)
    d = s.decide(cand)
    assert d is None


def test_decide_returns_none_when_close_is_none() -> None:
    s = MeanReversionStrategy(
        ema_span=24, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.05"),
    )
    s.observe(_c(0, "0.0001"))
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(cand)
    assert s.decide(cand) is None


def _mr() -> MeanReversionStrategy:
    return MeanReversionStrategy(
        ema_span=24, threshold_sigma=Decimal("0.5"), ratio_sigma=Decimal("0.05"),
    )


def test_ema_current_none_before_first_observe() -> None:
    assert _mr().ema_current is None


def test_ema_current_tracks_accumulator() -> None:
    mr = _mr()
    mr.observe(_c(0, "0.0003"))
    assert mr.ema_current == Decimal("0.0003")  # first observe seeds ema
    mr.observe(_c(3600_000, "0.0005"))
    alpha = Decimal(2) / Decimal(24 + 1)
    expected = alpha * Decimal("0.0005") + (Decimal("1") - alpha) * Decimal("0.0003")
    assert mr.ema_current == expected


def test_last_deviation_none_before_decide() -> None:
    mr = _mr()
    mr.observe(_c(0, "0.0003"))
    assert mr.last_deviation is None


def test_last_deviation_caches_most_recent_decide() -> None:
    mr = _mr()
    mr.observe(_c(0, "0.0003"))
    cand = _c(3600_000, "0.0004")
    mr.observe(cand)
    mr.decide(cand)
    ema = mr.ema_current
    assert ema is not None
    assert mr.last_deviation == (Decimal("0.0004") - ema) / ema


def test_param_grid_for_cell_uses_eda_ratio_sigma() -> None:
    grid = MeanReversionStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={
            "close_over_ema_sigma_24": Decimal("0.05"),
            "close_over_ema_sigma_168": Decimal("0.08"),
        },
    )
    assert len(grid) == 6
    spans = {p["ema_span"] for p in grid}
    thresholds = {p["threshold_sigma"] for p in grid}
    assert spans == {24, 168}
    assert thresholds == {Decimal("0.5"), Decimal("1.0"), Decimal("1.5")}
    for p in grid:
        if p["ema_span"] == 24:
            assert p["ratio_sigma"] == Decimal("0.05")
        else:
            assert p["ratio_sigma"] == Decimal("0.08")
