from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler
from pydantic_core import core_schema


class _AllowInfDecimal:
    """Pydantic type annotation that allows Decimal('Infinity') / Decimal('-Infinity').

    Pydantic's default Decimal validator rejects non-finite values. The sortino
    field legitimately produces +inf (no downside variance observed), so we need
    a passthrough validator that skips the finite-number check.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> object:
        return core_schema.no_info_plain_validator_function(
            lambda v: v if isinstance(v, Decimal) else Decimal(str(v))
        )


InfDecimal = Annotated[Decimal, _AllowInfDecimal]


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
    sortino: InfDecimal = Decimal("0")  # monthly-equity-sampled Sortino; 0 = untrustworthy (n<3); +inf = no downside
