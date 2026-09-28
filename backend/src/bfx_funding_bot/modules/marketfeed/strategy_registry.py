"""Per-cell live strategy state and the injected boundary reconstruction port."""
from __future__ import annotations

from typing import Protocol

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.strategy import (
    CellConfig,
    CellStrategyFactory,
    Strategy,
    StrategyBuildResult,
)


class BoundaryStrategyBuilder(Protocol):
    def __call__(
        self, *, cell: CellConfig, history: list[FundingCandle],
        ref_mts: int, budget_hours: int,
    ) -> StrategyBuildResult: ...


class StrategyRegistry:
    """Holds one live Strategy instance per cell (keyed by pair_id)."""

    def __init__(self, strategy_factory: CellStrategyFactory) -> None:
        self._strategy_factory = strategy_factory
        self._d: dict[str, Strategy] = {}

    def make(self, cell: CellConfig) -> Strategy:
        """Create an independent instance without registering it."""
        return self._strategy_factory(cell)

    def put(self, cell: CellConfig, strategy: Strategy) -> None:
        self._d[cell.pair_id] = strategy

    def get(self, cell: CellConfig) -> Strategy:
        try:
            return self._d[cell.pair_id]
        except KeyError as e:
            raise KeyError(f"no strategy registered for {cell.pair_id!r}") from e

    def items(self) -> list[tuple[str, Strategy]]:
        return list(self._d.items())
