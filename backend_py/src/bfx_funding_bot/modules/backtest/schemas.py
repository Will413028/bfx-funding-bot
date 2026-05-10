from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class LendDecision(BaseModel):
    """A simulated lending offer at a given candle.

    The strategy emits this; the engine then applies friction
    (fee, gap cost, fill probability based on spread vs market rate)
    in `modules.backtest.engine._apply_friction`.
    """

    model_config = ConfigDict(frozen=True)

    mts: int  # candle mts when decision was made
    rate: Decimal  # daily rate (matches Bitfinex funding rate semantics)
    period_days: int  # 2 = minimum, up to 120


class BacktestResult(BaseModel):
    """Output of a single backtest run.

    Tracks gross (pre-fee) and net (post-fee) returns separately so callers
    can attribute the gap between gross and net to the Bitfinex 15% fee.
    """

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    symbol: str
    start_mts: int
    end_mts: int
    n_candles: int
    gross_monthly_return_pct: Decimal  # before Bitfinex 15% fee
    net_monthly_return_pct: Decimal    # after fee + fill_prob + gap (what user keeps)
    max_drawdown_pct: Decimal           # tracked on net equity (conservative)
    n_trades: int
    fill_rate: Decimal                  # avg fill_prob across trades; 1.0 = always filled
