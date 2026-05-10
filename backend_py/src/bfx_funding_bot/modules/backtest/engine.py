from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
) -> BacktestResult:
    """Run a single strategy over a candle series and return summary metrics.

    Day-4 simplified accounting model:
      - Equity starts at 1.0 (treated as 1 unit of currency lent)
      - At each candle, ask the strategy for a LendDecision
      - If returned AND not mid-loan: lend for `period_days`, credit
        `rate * period_days` to equity (compounded)
      - Mid-loan cooldown is `period_days * 24` candles (assumes 1h candles)
      - monthly_return_pct = (final_equity - 1) / months_elapsed * 100
        where months_elapsed = total_hours / (24*30)
      - max_drawdown_pct = max((peak - trough) / peak * 100)

    Limitations (intentional for Checkpoint 2):
      - No partial fills / order book / FRR vs offered rate model
      - Assumes instant fill at candle close
      - Assumes 1h candles (period_days * 24 cooldown)
      - Single instrument
    """
    if not candles:
        return BacktestResult(
            strategy_name=strategy.name,
            symbol="",
            start_mts=0,
            end_mts=0,
            n_candles=0,
            monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0,
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    symbol = sorted_candles[0].symbol
    start_mts = sorted_candles[0].mts
    end_mts = sorted_candles[-1].mts

    equity = Decimal("1")
    peak = equity
    max_dd = Decimal("0")
    n_trades = 0
    cooldown_until_idx = -1

    for i, candle in enumerate(sorted_candles):
        if i <= cooldown_until_idx:
            continue
        decision = strategy.decide(candle)
        if decision is None:
            continue
        equity = equity * (Decimal("1") + decision.rate * Decimal(decision.period_days))
        n_trades += 1
        cooldown_until_idx = i + decision.period_days * 24
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

    if end_mts > start_mts:
        # Decimal-only arithmetic to avoid float precision drift
        total_hours = Decimal(end_mts - start_mts) / Decimal(3_600_000)
    else:
        total_hours = Decimal("0")
    months_elapsed = total_hours / Decimal("720") if total_hours > 0 else Decimal("0")

    if months_elapsed > 0:
        total_return = equity - Decimal("1")
        monthly_return = total_return / months_elapsed * Decimal("100")
    else:
        monthly_return = Decimal("0")

    return BacktestResult(
        strategy_name=strategy.name,
        symbol=symbol,
        start_mts=start_mts,
        end_mts=end_mts,
        n_candles=len(sorted_candles),
        monthly_return_pct=monthly_return,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades,
    )
