from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.strategies.always_frr import AlwaysFrrStrategy

FRR = Decimal("0.0003")


def _c(mts: int, close: str | None = "0.0004") -> FundingCandle:
    d = Decimal(close) if close is not None else None
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=d, close=d, high=d, low=d,
        volume=Decimal("100") if d is not None else None,
    )


def test_name_includes_period() -> None:
    assert AlwaysFrrStrategy(frr_at=lambda mts: FRR).name == "always_frr_p2"


def test_decide_lends_at_frr_per_day_rate() -> None:
    s = AlwaysFrrStrategy(frr_at=lambda mts: FRR)
    d = s.decide(_c(7))
    assert d is not None
    assert d.rate == FRR
    assert d.period_days == 2
    assert d.mts == 7


def test_decide_returns_none_when_frr_unavailable() -> None:
    s = AlwaysFrrStrategy(frr_at=lambda mts: None)
    assert s.decide(_c(0)) is None


def test_decide_lends_even_when_close_is_none() -> None:
    # FRR parking does not need a candle close to quote; the engine's friction
    # model degrades to fill=1 when no market rate exists.
    s = AlwaysFrrStrategy(frr_at=lambda mts: FRR)
    d = s.decide(_c(0, close=None))
    assert d is not None
    assert d.rate == FRR
