from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class AlwaysFRRStrategy(Strategy):
    """Baseline: at every candle with a close rate, lend at that rate for
    `period_days`. Naive — no awareness of FRR vs offered rate, no order book.

    Exists ONLY to validate that the backtest engine produces a number
    end-to-end (Checkpoint 2 pass).
    """

    def __init__(self, period_days: int = 2) -> None:
        self._period_days = period_days

    @property
    def name(self) -> str:
        return f"always_frr_p{self._period_days}"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        return LendDecision(
            mts=candle.mts,
            rate=candle.close,
            period_days=self._period_days,
        )
