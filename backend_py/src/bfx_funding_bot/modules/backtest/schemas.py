from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class LendDecision(BaseModel):
    """A simulated lending offer at a given candle.

    Day-4 simplification: lend is treated as instantly filled at the close
    rate of the candle. Real fill probability vs FRR is deferred to Stage 2.
    """

    model_config = ConfigDict(frozen=True)

    mts: int  # candle mts when decision was made
    rate: Decimal  # daily rate (matches Bitfinex funding rate semantics)
    period_days: int  # 2 = minimum, up to 120


class BacktestResult(BaseModel):
    """Output of a single backtest run."""

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    symbol: str
    start_mts: int
    end_mts: int
    n_candles: int
    monthly_return_pct: Decimal  # e.g. 0.45 = 0.45% per month
    max_drawdown_pct: Decimal  # e.g. 0.05 = 5%
    n_trades: int
