from decimal import Decimal

from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(start_mts: int, n: int, close: str, step_ms: int = 3_600_000) -> list[FundingCandle]:
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=start_mts + i * step_ms, close=Decimal(close))
        for i in range(n)
    ]


def test_evaluate_oos_windows_pairs_outcomes_per_window() -> None:
    # two windows, flat rate; AlwaysFRR for both arms -> identical outcomes,
    # one WindowOutcome per window, aligned by month_mts.
    # _candles(0, 1000, ...) spans mts 0..999*3_600_000 = 3_596_400_000
    # window test range 500_000_000..900_000_000 is fully within that span.
    candles = _candles(0, 1000, "0.0003")
    windows = [
        WfoWindow(train_start_mts=0, train_end_mts=499_999_999,
                  test_start_mts=500_000_000, test_end_mts=900_000_000),
    ]
    strat_out, base_out = evaluate_oos_windows(
        candles, windows, make_strategy=lambda: AlwaysFRRStrategy(period_days=2)
    )
    assert len(strat_out) == len(base_out) == 1
    assert strat_out[0].month_mts == base_out[0].month_mts == 500_000_000
    assert strat_out[0].net_monthly == base_out[0].net_monthly
