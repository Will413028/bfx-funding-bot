"""Strategy factory + per-cell instance registry.

Strategy plugin abstraction is Phase 5+ scope.
Phase 4.1: hardcoded if/elif for 2 strategy enums.

MeanReversionStrategy takes ema_span: int (EMA window length),
but cells.yaml / CellConfig stores ema_alpha: float (smoothing factor).
Conversion: alpha = 2 / (span + 1)  ->  span = round(2 / alpha - 1).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName


class _Strategy(Protocol):
    @property
    def name(self) -> str: ...
    def observe(self, candle: FundingCandle) -> None: ...
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...


def _ema_alpha_to_span(alpha: float) -> int:
    """Convert EMA smoothing factor to equivalent span (rounds to nearest int).

    alpha = 2 / (span + 1)  →  span = 2/alpha - 1
    """
    return max(1, round(2.0 / alpha - 1))


def build_strategy(cell: CellConfig) -> _Strategy:
    """Instantiate the correct Strategy subclass from a CellConfig."""
    if cell.strategy == StrategyName.MEAN_REVERSION:
        p = cell.params
        ema_span = _ema_alpha_to_span(float(p["ema_alpha"]))
        return MeanReversionStrategy(
            ema_span=ema_span,
            threshold_sigma=Decimal(str(p["threshold_sigma"])),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
        )
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        p = cell.params
        return RatePercentileStrategy(
            percentile=int(p["percentile"]),
            lookback_hours=int(p["lookback_hours"]),
        )
    raise ValueError(f"unsupported strategy {cell.strategy!r}")


class StrategyRegistry:
    """Holds one live Strategy instance per cell (keyed by pair_id)."""

    def __init__(self) -> None:
        self._d: dict[str, _Strategy] = {}

    def put(self, cell: CellConfig, strategy: _Strategy) -> None:
        self._d[cell.pair_id] = strategy

    def get(self, cell: CellConfig) -> _Strategy:
        try:
            return self._d[cell.pair_id]
        except KeyError as e:
            raise KeyError(f"no strategy registered for {cell.pair_id!r}") from e

    def items(self) -> list[tuple[str, _Strategy]]:
        return list(self._d.items())
