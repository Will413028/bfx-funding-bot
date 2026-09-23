"""AlwaysFRR baseline arm (research-gated, no live path).

Models the free alternative every user already has: leave the balance on FRR
auto-renew. At every candle it offers the as-of per-day FRR rate (funding_stats
frr x 365 via FrrSeries) for the minimum period. The engine's friction model
applies (FRR above market close -> linear fill decay), matching how the other
arms are scored — it does NOT get a guaranteed fill.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class AlwaysFrrStrategy(Strategy):
    """Lend at the as-of FRR per-day rate on every candle.

    `frr_at` is an as-of lookup (candle mts -> per-day FRR rate, e.g.
    FrrSeries.at). Returns None (idle) only when FRR data is unavailable.
    """

    def __init__(
        self,
        frr_at: Callable[[int], Decimal | None],
        period_days: int = 2,
    ) -> None:
        self._frr_at = frr_at
        self._period_days = period_days

    @property
    def name(self) -> str:
        return f"always_frr_p{self._period_days}"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        frr = self._frr_at(candle.mts)
        if frr is None:
            return None
        return LendDecision(mts=candle.mts, rate=frr, period_days=self._period_days)
