"""The strategy may observe one series while fills are priced off another.

Needed to measure candle distortion honestly: live saw a distorted close and
quoted from it, but the market settled at the true value. Feeding one series to
both sides makes the error cancel — spread_pct becomes 0 and the distortion
looks harmless, which is exactly how the first L4 run produced a false all-clear.
"""
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy import (
    AlwaysMarketRateStrategy,
)

_HOUR = 3_600_000
_LINEAR_CONFIG = BacktestConfig(fill_model="linear-baseline")


def _series(close: str, n: int = 120) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1700000000000 + i * _HOUR,
            open=Decimal(close), close=Decimal(close),
            high=Decimal(close), low=Decimal(close), volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_quoting_off_a_distorted_series_hurts_fills() -> None:
    """Quote from an inflated close, get filled against the true one -> worse fills."""
    observed = _series("0.0002")  # what the bot saw
    market = _series("0.0001")  # where the market actually was

    naive = run_backtest(observed, AlwaysMarketRateStrategy(period_days=2), _LINEAR_CONFIG)
    dual = run_backtest(
        observed, AlwaysMarketRateStrategy(period_days=2), _LINEAR_CONFIG, market_candles=market
    )

    assert naive.fill_rate == Decimal("1"), "single series: spread is 0 by construction"
    assert dual.fill_rate < naive.fill_rate, (
        "quoting 100% above the true market must reduce fill probability"
    )
    assert dual.net_monthly_return_pct < naive.net_monthly_return_pct


def test_identical_series_matches_single_series_behaviour() -> None:
    """Passing the same series twice must change nothing — guards the default path."""
    candles = _series("0.00015")

    single = run_backtest(candles, AlwaysMarketRateStrategy(period_days=2), _LINEAR_CONFIG)
    dual = run_backtest(
        candles, AlwaysMarketRateStrategy(period_days=2), _LINEAR_CONFIG, market_candles=list(candles)
    )

    assert dual.fill_rate == single.fill_rate
    assert dual.net_monthly_return_pct == single.net_monthly_return_pct
    assert dual.n_trades == single.n_trades


def test_evaluate_oos_windows_forwards_the_market_series() -> None:
    """The OOS layer must pass the market series through, or L4 stays useless."""
    from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
    from bfx_funding_bot.modules.backtest.wfo import WfoWindow

    observed = _series("0.0002", n=400)
    market = _series("0.0001", n=400)
    window = WfoWindow(
        train_start_mts=observed[0].mts,
        train_end_mts=observed[200].mts - 1,
        test_start_mts=observed[200].mts,
        test_end_mts=observed[-1].mts,
    )

    def _mk():
        return AlwaysMarketRateStrategy(period_days=2)

    single, _ = evaluate_oos_windows(observed, [window], _mk, config=_LINEAR_CONFIG, fill_model=None)
    dual, _ = evaluate_oos_windows(observed, [window], _mk, config=_LINEAR_CONFIG, fill_model=None, market_candles=market)

    assert single[0].fill_rate > dual[0].fill_rate
