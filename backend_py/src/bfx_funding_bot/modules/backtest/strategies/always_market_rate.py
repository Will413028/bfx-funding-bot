from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class AlwaysMarketRateStrategy(Strategy):
    """Baseline: at every candle with a close rate, lend at that market rate
    (funding_candles.close, the canonical per-day market funding rate) for
    `period_days`. Naive — no order-book awareness, no offered-vs-market spread.

    This is the passive benchmark the active strategy arms are scored against
    (and it validates the backtest engine end-to-end — Checkpoint 2). It lends at
    the market rate, NOT funding_stats.frr, which is not a market-rate proxy.
    """

    def __init__(self, period_days: int = 2) -> None:
        self._period_days = period_days

    @property
    def name(self) -> str:
        return f"always_market_rate_p{self._period_days}"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        return LendDecision(
            mts=candle.mts,
            rate=candle.close,
            period_days=self._period_days,
        )
