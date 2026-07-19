"""MR-with-FRR-floor backtest variant (research-gated, no live path).

Answers the 2026-07-06 profit review SKIP-FRR-parking open question (§3):
plain MeanReversion returns None (idle, earns 0) when close falls below the
EMA lower band; this variant instead parks the budget at FRR for the minimum
period. Non-skip candles behave exactly like MeanReversionStrategy (rate =
candle close). The engine's friction model still applies to the parked offer
(rate above market close -> linear fill decay), so FRR parking is NOT assumed
to always fill.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class MeanReversionFrrFloorStrategy(MeanReversionStrategy):
    """MeanReversion, but the SKIP branch parks at FRR instead of going idle.

    `frr_at` is an as-of lookup (candle mts -> per-day FRR rate, e.g.
    FrrSeries.at). When it returns None during a skip (coverage gap), the
    strategy degrades to plain-MR behavior: idle.
    """

    def __init__(
        self,
        ema_span: int,
        threshold_sigma: Decimal,
        ratio_sigma: Decimal,
        frr_at: Callable[[int], Decimal | None],
    ) -> None:
        super().__init__(
            ema_span=ema_span, threshold_sigma=threshold_sigma, ratio_sigma=ratio_sigma
        )
        self._frr_at = frr_at

    @property
    def name(self) -> str:
        return f"mean_reversion_frr_floor_ema{self._ema_span}_sigma{self._threshold_sigma}"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        decision = super().decide(candle)
        if decision is not None:
            return decision
        # None from the parent is either "not ready" (no close / no EMA) or the
        # SKIP branch. Only the SKIP branch parks at FRR.
        if candle.close is None or self._ema is None or self._ema == 0:
            return None
        frr = self._frr_at(candle.mts)
        if frr is None:
            return None
        return LendDecision(mts=candle.mts, rate=frr, period_days=2)
