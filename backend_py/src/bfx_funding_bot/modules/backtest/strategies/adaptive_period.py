from __future__ import annotations

from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

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

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        raise NotImplementedError  # implemented in Task 3
