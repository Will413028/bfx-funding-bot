from __future__ import annotations

from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.lend_decision import LendDecision
from bfx_funding_bot.modules.strategy._internal.strategies.base import Strategy
from bfx_funding_bot.modules.strategy.contracts import AdaptivePeriodDiagnostics

_PERIOD_FLOOR = 2       # Bitfinex funding offer minimum period (days)
_PERIOD_MAX = 120       # Bitfinex funding offer maximum period (days)


class AdaptivePeriodStrategy(Strategy):
    """Always lend at the market rate (candle.close); vary the lock PERIOD by
    the rate's deviation from its EMA.

    Same mean-reversion thesis as MeanReversion, expressed through duration
    instead of gating: rate high vs trend -> expected to revert down -> lock
    LONG (ride the locked-high rate down); rate at/below trend -> lock SHORT
    (re-price upward frequently). A spike (deviation > band2) is the top tier
    -> lock longest. Never pauses (maximizes bot-vs-idle).

    The fill model pins the optimal posting rate to candle.close, so rate is
    always candle.close; period is the only alpha lever, and the one no other
    strategy uses.
    """

    def __init__(
        self,
        ema_span: int,
        ratio_sigma: Decimal,
        t1: Decimal,
        t2: Decimal,
        p_mid: int,
        p_long: int,
    ) -> None:
        self._ema_span = ema_span
        self._ratio_sigma = ratio_sigma
        self._t1 = t1
        self._t2 = t2
        self._p_mid = p_mid
        self._p_long = p_long
        self._alpha = Decimal(2) / Decimal(ema_span + 1)
        self._ema: Decimal | None = None
        self._samples = 0
        self._last_period: int | None = None

    @property
    def name(self) -> str:
        return f"adaptive_period_ema{self._ema_span}_t{self._t1}_{self._t2}"

    @property
    def ema_current(self) -> Decimal | None:
        return self._ema

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def window_filled(self) -> bool:
        return self._samples >= self._ema_span

    @property
    def last_period(self) -> int | None:
        return self._last_period

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is None:
            return
        self._samples += 1
        if self._ema is None:
            self._ema = candle.close
        else:
            self._ema = (
                self._alpha * candle.close
                + (Decimal("1") - self._alpha) * self._ema
            )

    def _period_for(self, close: Decimal) -> int:
        # Warmup or undefined EMA -> safest shortest lock.
        if self._ema is None or self._ema == 0 or self._samples < self._ema_span:
            return _PERIOD_FLOOR
        deviation = (close - self._ema) / self._ema
        band1 = self._t1 * self._ratio_sigma
        band2 = self._t2 * self._ratio_sigma
        if deviation <= band1:
            period = _PERIOD_FLOOR
        elif deviation <= band2:
            period = self._p_mid
        else:
            period = self._p_long
        return max(_PERIOD_FLOOR, min(_PERIOD_MAX, period))

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        period = self._period_for(candle.close)
        self._last_period = period
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=period)

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Deployed candidate: ema_span=24, band (t1=0.5, t2=2.0), p_long=14.

        Provenance:
        - ema_span=24 chosen by 2026-06-04 band sweep (span-24 tested; span-168
          not evaluated for AP, so grid is narrowed to the deployed candidate).
        - Band (t1=0.5, t2=2.0) chosen by 2026-06-04 band sweep as optimal
          trade-off: +median alpha vs AlwaysMarketRate with lowest fill risk.
        - p_long=14 chosen by 2026-06-03 p_long sweep: p14->p30 yields only
          ~+20% median alpha but ~+90% mean (fat-tail driven); p14 retains
          80-84% of robust edge while halving fat-tail + 30-day credit risk.
        - p_mid=7 fixed as midpoint between floor (2) and p_long (14).
        - ratio_sigma injected from EDA close/EMA sigma for ema_span=24.
        """
        sigma_24 = eda.get("close_over_ema_sigma_24", Decimal("0.05"))
        return [
            {
                "ema_span": 24, "ratio_sigma": sigma_24,
                "t1": Decimal("0.5"), "t2": Decimal("2.0"),
                "p_mid": 7, "p_long": 14,
            }
        ]

    def diagnostics(self) -> AdaptivePeriodDiagnostics:
        return AdaptivePeriodDiagnostics(
            ema_current=self.ema_current,
            window_filled=self.window_filled,
        )
