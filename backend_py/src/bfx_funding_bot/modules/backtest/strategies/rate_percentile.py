from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

import numpy as np

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class RatePercentileStrategy(Strategy):
    """Only lend when current close >= percentile of last N candles' closes.

    Stateful: observe() appends close to a rolling deque(maxlen=lookback_hours).
    decide() returns a LendDecision (period=2) iff the window is full AND the
    current close is at-or-above the configured percentile of the window.
    """

    def __init__(self, percentile: int, lookback_hours: int) -> None:
        self._percentile = percentile
        self._lookback_hours = lookback_hours
        self._window: deque[Decimal] = deque(maxlen=lookback_hours)
        self._last_threshold: Decimal | None = None

    @property
    def name(self) -> str:
        return f"rate_percentile_p{self._percentile}_n{self._lookback_hours}"

    @property
    def last_threshold(self) -> Decimal | None:
        return self._last_threshold

    @property
    def window_filled(self) -> bool:
        return len(self._window) >= self._lookback_hours

    @property
    def window_values(self) -> tuple[Decimal, ...]:
        """Read-only snapshot of the rolling window (for parity diagnostics)."""
        return tuple(self._window)

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is not None:
            self._window.append(candle.close)

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        if len(self._window) < self._lookback_hours:
            return None  # warmup
        threshold = Decimal(str(float(
            np.percentile([float(x) for x in self._window], self._percentile)
        )))
        self._last_threshold = threshold
        if candle.close >= threshold:
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
        return None

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Lookback grid depends on ACF at lag 168h (per EDA result).
        - acf_168h_pass True (corr >= 0.3): {168, 720}
        - acf_168h_pass False:              {168} only (drop 720)
        """
        looks = [168, 720] if eda.get("acf_168h_pass", False) else [168]
        return [
            {"percentile": p, "lookback_hours": n}
            for p in (25, 50, 75) for n in looks
        ]
