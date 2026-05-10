from decimal import Decimal

from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles_constant_rate(rate: str, n: int = 720) -> list[FundingCandle]:
    """720 hourly candles ≈ 30 days. Constant rate makes math hand-checkable."""
    return [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704067200000 + i * 3_600_000,  # +1h per candle
            open=Decimal(rate),
            close=Decimal(rate),
            high=Decimal(rate),
            low=Decimal(rate),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_run_backtest_constant_rate_produces_expected_monthly_return() -> None:
    """At constant 0.0001 daily rate over 30 days lending continuously,
    monthly return ~= 30 days * 0.0001 = 0.003 = 0.3% (compounded slightly higher).
    """
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    result = run_backtest(candles, strategy)

    assert result.strategy_name == "always_frr_p2"
    assert result.symbol == "fUST"
    assert result.n_candles == 720
    # 720 hours = 30 days; period 2 days = 15 lends. Compound: 1.0002^15 ≈ 1.003.
    assert abs(result.monthly_return_pct - Decimal("0.3")) < Decimal("0.01")
    assert result.n_trades == 15
    assert result.max_drawdown_pct == Decimal("0")


def test_run_backtest_handles_empty_candles() -> None:
    candles: list[FundingCandle] = []
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles, strategy)
    assert result.n_candles == 0
    assert result.n_trades == 0
    assert result.monthly_return_pct == Decimal("0")
    assert result.max_drawdown_pct == Decimal("0")


def test_run_backtest_skips_candles_with_no_close() -> None:
    candles = _candles_constant_rate("0.0001", n=720)
    candles_with_holes = []
    for i, c in enumerate(candles):
        if i % 100 == 0:
            candles_with_holes.append(c.model_copy(update={"close": None}))
        else:
            candles_with_holes.append(c)
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles_with_holes, strategy)
    assert result.n_candles == 720
    assert result.n_trades > 0
