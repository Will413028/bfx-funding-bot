"""Composition-only strategy factories. Import from apps, never module facades."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.candles.reindex import reindex_and_ffill
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy._internal.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.always_frr import AlwaysFrrStrategy
from bfx_funding_bot.modules.strategy._internal.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.base import Strategy as StrategyABC
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.mean_reversion_frr_floor import (
    MeanReversionFrrFloorStrategy,
)
from bfx_funding_bot.modules.strategy._internal.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.strategy.config import CellConfig
from bfx_funding_bot.modules.strategy.contracts import (
    ResearchStrategySpec,
    Strategy,
    StrategyBuildResult,
    StrategyName,
)


def build_strategy(cell: CellConfig) -> Strategy:
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
    """Build fresh state, leaving the boundary slot for observe + decide."""
    strategy = build_strategy(cell)
    if not history:
        return StrategyBuildResult(strategy=strategy, observed_count=0)
    filled = reindex_and_ffill(history, ref_mts=ref_mts, max_gap_hours=budget_hours)
    observed = [fc.candle for fc in filled[:-1] if fc.candle is not None]
    for c in observed:
        strategy.observe(c)
    return StrategyBuildResult(strategy=strategy, observed_count=len(observed))


@dataclass(frozen=True)
class _ResearchStrategySpec:
    strategy_class: type[StrategyABC]

    @property
    def name(self) -> str:
        return self.strategy_class.__name__

    def param_grid_for_cell(
        self, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # Baselines have one default variant; their legacy ABC grid raises.
        if self.strategy_class in (AlwaysFrrStrategy, AlwaysMarketRateStrategy):
            return [{}]
        return self.strategy_class.param_grid_for_cell(symbol, period_agg, eda)

    def create(self, **params: Any) -> Strategy:
        return self.strategy_class(**params)


RESEARCH_STRATEGIES: tuple[ResearchStrategySpec, ...] = tuple(
    _ResearchStrategySpec(cls)
    for cls in (
        AdaptivePeriodStrategy, AlwaysFrrStrategy, AlwaysMarketRateStrategy,
        MeanReversionStrategy, MeanReversionFrrFloorStrategy, RatePercentileStrategy,
    )
)
