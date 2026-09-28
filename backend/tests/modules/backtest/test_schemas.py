from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.strategy import LendDecision


def test_backtest_result_construction() -> None:
    result = BacktestResult(
        strategy_name="always_frr",
        symbol="fUST",
        start_mts=1704067200000,
        end_mts=1706745600000,
        n_candles=720,
        gross_monthly_return_pct=Decimal("0.45"),
        net_monthly_return_pct=Decimal("0.3825"),
        max_drawdown_pct=Decimal("0.05"),
        n_trades=30,
        fill_rate=Decimal("1.0"),
    )
    assert result.strategy_name == "always_frr"
    assert result.gross_monthly_return_pct == Decimal("0.45")
    assert result.net_monthly_return_pct == Decimal("0.3825")
    assert result.max_drawdown_pct == Decimal("0.05")
    assert result.fill_rate == Decimal("1.0")


def test_lend_decision_construction() -> None:
    d = LendDecision(mts=1704067200000, rate=Decimal("0.000123"), period_days=2)
    assert d.mts == 1704067200000
    assert d.rate == Decimal("0.000123")
    assert d.period_days == 2


def test_backtest_result_has_sortino_field() -> None:
    r = BacktestResult(
        strategy_name="x", symbol="fUST", start_mts=0, end_mts=1,
        n_candles=0,
        gross_monthly_return_pct=Decimal("0"),
        net_monthly_return_pct=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        n_trades=0, fill_rate=Decimal("0"),
        sortino=Decimal("1.5"),
    )
    assert r.sortino == Decimal("1.5")
