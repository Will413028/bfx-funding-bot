from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision


def test_backtest_result_construction() -> None:
    result = BacktestResult(
        strategy_name="always_frr",
        symbol="fUST",
        start_mts=1704067200000,
        end_mts=1706745600000,
        n_candles=720,
        monthly_return_pct=Decimal("0.45"),
        max_drawdown_pct=Decimal("0.05"),
        n_trades=30,
    )
    assert result.strategy_name == "always_frr"
    assert result.monthly_return_pct == Decimal("0.45")
    assert result.max_drawdown_pct == Decimal("0.05")


def test_lend_decision_construction() -> None:
    d = LendDecision(mts=1704067200000, rate=Decimal("0.000123"), period_days=2)
    assert d.mts == 1704067200000
    assert d.rate == Decimal("0.000123")
    assert d.period_days == 2
