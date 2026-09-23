from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class _StubStrategy(Strategy):
    @property
    def name(self) -> str:
        return "stub"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        return None


def _candle() -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )


def test_observe_default_is_noop() -> None:
    s = _StubStrategy()
    # Should not raise
    s.observe(_candle())


def test_param_grid_for_cell_default_raises() -> None:
    with pytest.raises(NotImplementedError):
        _StubStrategy.param_grid_for_cell("fUST", "p2", eda={})
