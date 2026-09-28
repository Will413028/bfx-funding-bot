from __future__ import annotations

from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.lend_decision import LendDecision
from bfx_funding_bot.modules.strategy._internal.strategies.base import Strategy
from bfx_funding_bot.modules.strategy.contracts import MeanReversionDiagnostics


class MeanReversionStrategy(Strategy):
    """Pause lending when close has fallen too far below EMA.

    Stateful: observe() incrementally updates EMA. decide() emits a
    LendDecision iff the current close is at-or-above the lower band
    (-threshold_sigma * ratio_sigma) relative to EMA.

    ratio_sigma is the historical std of (close/EMA - 1); injected per
    cell from EDA by param_grid_for_cell so the threshold scales to
    the cell's volatility.
    """

    def __init__(
        self,
        ema_span: int,
        threshold_sigma: Decimal,
        ratio_sigma: Decimal,
    ) -> None:
        self._ema_span = ema_span
        self._threshold_sigma = threshold_sigma
        self._ratio_sigma = ratio_sigma
        self._alpha = Decimal(2) / Decimal(ema_span + 1)
        self._ema: Decimal | None = None
        self._last_deviation: Decimal | None = None

    @property
    def name(self) -> str:
        return f"mean_reversion_ema{self._ema_span}_sigma{self._threshold_sigma}"

    @property
    def ema_current(self) -> Decimal | None:
        return self._ema

    @property
    def last_deviation(self) -> Decimal | None:
        return self._last_deviation

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is None:
            return
        if self._ema is None:
            self._ema = candle.close
        else:
            self._ema = (
                self._alpha * candle.close
                + (Decimal("1") - self._alpha) * self._ema
            )

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None or self._ema is None or self._ema == 0:
            return None
        deviation = (candle.close - self._ema) / self._ema
        self._last_deviation = deviation
        lower_band = -self._threshold_sigma * self._ratio_sigma
        if deviation < lower_band:
            return None
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Grid: ema_span in {24, 168} x threshold_sigma in {0.5, 1.0, 1.5}.
        ratio_sigma is injected per ema_span from EDA close/EMA sigma stats.
        """
        sigma_24 = eda.get("close_over_ema_sigma_24", Decimal("0.05"))
        sigma_168 = eda.get("close_over_ema_sigma_168", Decimal("0.05"))
        spans = [(24, sigma_24), (168, sigma_168)]
        return [
            {"ema_span": span, "threshold_sigma": ts, "ratio_sigma": sigma}
            for span, sigma in spans
            for ts in (Decimal("0.5"), Decimal("1.0"), Decimal("1.5"))
        ]

    def diagnostics(self) -> MeanReversionDiagnostics:
        return MeanReversionDiagnostics(
            ema_current=self.ema_current,
            last_deviation=self.last_deviation,
        )
