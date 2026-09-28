from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy import AlwaysMarketRateStrategy, LendDecision, Strategy

_LINEAR_CONFIG = BacktestConfig(fill_model="linear-baseline")


def _candles(start_mts: int, n: int, close: str, step_ms: int = 3_600_000) -> list[FundingCandle]:
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=start_mts + i * step_ms, close=Decimal(close))
        for i in range(n)
    ]


class _NeverLendsStrategy(Strategy):
    """Test stub: always declines to lend (returns None from decide).

    Used to verify that evaluate_oos_windows runs candidate and baseline
    arms independently — a strategy that produces zero trades gives
    net_monthly=0, which must differ from AlwaysMarketRate's positive return.
    """

    @property
    def name(self) -> str:
        return "never_lends"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        return None


def test_evaluate_oos_windows_pairs_outcomes_per_window() -> None:
    # one window, flat rate; AlwaysMarketRate for both arms -> identical outcomes,
    # one WindowOutcome per window, aligned by month_mts.
    # _candles(0, 1000, ...) spans mts 0..999*3_600_000 = 3_596_400_000
    # window test range 500_000_000..900_000_000 is fully within that span.
    candles = _candles(0, 1000, "0.0003")
    windows = [
        WfoWindow(train_start_mts=0, train_end_mts=499_999_999,
                  test_start_mts=500_000_000, test_end_mts=900_000_000),
    ]
    strat_out, base_out = evaluate_oos_windows(
        candles, windows, make_strategy=lambda: AlwaysMarketRateStrategy(period_days=2),
        config=_LINEAR_CONFIG, fill_model=None,
    )
    assert len(strat_out) == len(base_out) == 1
    assert strat_out[0].month_mts == base_out[0].month_mts == 500_000_000
    assert strat_out[0].net_monthly == base_out[0].net_monthly


def test_evaluate_oos_windows_candidate_differs_from_baseline() -> None:
    """Candidate arm and baseline arm run independently and can differ.

    The candidate is _NeverLendsStrategy (always returns None -> zero trades
    -> net_monthly == 0). The baseline is AlwaysMarketRateStrategy(period_days=2)
    which lends at every non-cooldown candle -> n_trades > 0 -> net_monthly > 0.

    Mechanism: the engine only accumulates equity when decide() returns a
    LendDecision. _NeverLendsStrategy never fires, so net_equity stays at 1.0
    and net_monthly_return_pct stays at exactly 0. AlwaysMarketRate fires repeatedly
    (cooldown = 2*24 + 1 = 49 candles per trade; the ~111-hour test window
    contains several non-cooldown candles) and compounds positive returns.

    This test catches the regression where make_strategy() is mistakenly fed
    to BOTH run_backtest calls: both arms would produce identical non-zero
    results and the assertion strat_out[i].net_monthly == 0 would fail.

    Uses two windows to also verify list length and month_mts ordering.
    """
    candles = _candles(0, 1000, "0.0003")
    windows = [
        WfoWindow(train_start_mts=0, train_end_mts=499_999_999,
                  test_start_mts=500_000_000, test_end_mts=700_000_000),
        WfoWindow(train_start_mts=100_000_000, train_end_mts=699_999_999,
                  test_start_mts=700_000_000, test_end_mts=900_000_000),
    ]
    strat_out, base_out = evaluate_oos_windows(
        candles, windows, make_strategy=_NeverLendsStrategy,
        config=_LINEAR_CONFIG, fill_model=None,
    )

    # Both lists must have one outcome per window, in window order.
    assert len(strat_out) == len(base_out) == 2
    assert strat_out[0].month_mts == base_out[0].month_mts == 500_000_000
    assert strat_out[1].month_mts == base_out[1].month_mts == 700_000_000

    # Candidate (NeverLends) -> zero trades -> net_monthly exactly 0 for all windows.
    assert strat_out[0].n_trades == 0
    assert strat_out[1].n_trades == 0
    assert strat_out[0].net_monthly == Decimal("0")
    assert strat_out[1].net_monthly == Decimal("0")

    # Baseline (AlwaysMarketRate) -> fills repeatedly -> positive net_monthly in both windows.
    assert base_out[0].n_trades > 0
    assert base_out[1].n_trades > 0
    assert base_out[0].net_monthly > Decimal("0")
    assert base_out[1].net_monthly > Decimal("0")

    # Core contract: the two arms produce DIFFERENT outcomes.
    assert strat_out[0].net_monthly != base_out[0].net_monthly
    assert strat_out[1].net_monthly != base_out[1].net_monthly


def test_evaluate_oos_windows_requires_explicit_config() -> None:
    candles = _candles(0, 300, "0.0003")
    windows = [
        WfoWindow(
            train_start_mts=0,
            train_end_mts=100_000_000,
            test_start_mts=200_000_000,
            test_end_mts=300_000_000,
        )
    ]

    with pytest.raises(TypeError):
        evaluate_oos_windows(
            candles,
            windows,
            make_strategy=lambda: AlwaysMarketRateStrategy(period_days=2),
        )
