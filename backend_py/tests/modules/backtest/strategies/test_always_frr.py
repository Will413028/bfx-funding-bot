from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(close: str | None) -> FundingCandle:
    return FundingCandle(
        symbol="fUST",
        timeframe="1h",
        period_agg="p2",
        mts=1704067200000,
        open=Decimal("0.0001"),
        close=Decimal(close) if close is not None else None,
        high=Decimal("0.0002"),
        low=Decimal("0.00005"),
        volume=Decimal("100.0"),
    )


def test_always_frr_lends_at_close_rate() -> None:
    s = AlwaysFRRStrategy(period_days=2)
    decision = s.decide(_candle("0.000123"))
    assert decision is not None
    assert decision.rate == Decimal("0.000123")
    assert decision.period_days == 2


def test_always_frr_skips_candle_with_no_close() -> None:
    s = AlwaysFRRStrategy(period_days=2)
    assert s.decide(_candle(None)) is None


def test_always_frr_name() -> None:
    s = AlwaysFRRStrategy(period_days=2)
    assert s.name == "always_frr_p2"


def test_always_frr_respects_period() -> None:
    s = AlwaysFRRStrategy(period_days=30)
    decision = s.decide(_candle("0.001"))
    assert decision is not None
    assert decision.period_days == 30
    assert s.name == "always_frr_p30"
