"""Strategy factory + per-cell instance registry.

Strategy plugin abstraction is Phase 5+ scope.
Phase 4.1: hardcoded if/elif for 2 strategy enums.

MeanReversionStrategy takes ema_span: int (EMA window length).
cells.yaml / CellConfig stores ema_span directly (int).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName


class _Strategy(Protocol):
    @property
    def name(self) -> str: ...
    def observe(self, candle: FundingCandle) -> None: ...
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...


@dataclass(frozen=True)
class StrategyBuildResult:
    strategy: _Strategy
    observed_count: int


def build_strategy(cell: CellConfig) -> _Strategy:
    """Instantiate the correct Strategy subclass from a CellConfig."""
    if cell.strategy == StrategyName.MEAN_REVERSION:
        p = cell.params
        return MeanReversionStrategy(
            ema_span=int(p["ema_span"]),
            threshold_sigma=Decimal(str(p["threshold_sigma"])),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
        )
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        p = cell.params
        return RatePercentileStrategy(
            percentile=int(p["percentile"]),
            lookback_hours=int(p["lookback_hours"]),
        )
    if cell.strategy == StrategyName.ADAPTIVE_PERIOD:
        p = cell.params
        return AdaptivePeriodStrategy(
            ema_span=int(p["ema_span"]),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
            t1=Decimal(str(p["t1"])),
            t2=Decimal(str(p["t2"])),
            p_mid=int(p["p_mid"]),
            p_long=int(p["p_long"]),
        )
    raise ValueError(f"unsupported strategy {cell.strategy!r}")


def build_strategy_at_boundary(
    *,
    cell: CellConfig,
    history: list[FundingCandle],
    ref_mts: int,
    budget_hours: int,
) -> StrategyBuildResult:
    """Phase 4.3 LOCF single source of truth — build a fresh strategy state by
    applying reindex_and_ffill to `history` (LOCF over the hourly grid ending
    at ref_mts) and observing every non-None filled slot EXCEPT the boundary
    slot (the one at ref_mts).

    The boundary slot is intentionally dropped: in the live (daemon) path the
    scheduler delivers the boundary candle to signal_engine which calls
    ExtractedSignal.extract — and extract has an observe side effect. Calling
    this function to populate state, then extract on the boundary, produces
    the same final state via the same sequence of observe() calls regardless
    of which code path (warmup, divergence replay) invoked it.

    Used by:
    - warmup.py (warmup_cell): populate StrategyRegistry once at daemon start.
    - divergence_reporter.py (DivergenceReporter.check): rebuild the
      reference strategy each tick for CP1 byte-equivalence comparison.

    Both call sites pass the same (history, ref_mts, budget_hours) — given
    those, this function is deterministic and side-effect-free aside from
    constructing a Strategy. That property is what guarantees CP1
    byte-equivalence between live and replay across the warmup → tick →
    replay sequence.
    """
    strategy = build_strategy(cell)
    if not history:
        return StrategyBuildResult(strategy=strategy, observed_count=0)
    filled = reindex_and_ffill(history, ref_mts=ref_mts, max_gap_hours=budget_hours)
    observed = [fc.candle for fc in filled[:-1] if fc.candle is not None]
    for c in observed:
        strategy.observe(c)
    return StrategyBuildResult(strategy=strategy, observed_count=len(observed))


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
