from abc import ABC, abstractmethod

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class Strategy(ABC):
    """Abstract base for backtest strategies.

    Day-4 contract: strategy looks at current candle and returns either
    a LendDecision (place an offer that fills at this candle's close rate
    for the given period) or None (do nothing this candle).
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...
