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
