from abc import ABC, abstractmethod
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class Strategy(ABC):
    """Abstract base for backtest strategies.

    Stateful contract (Phase 3b):
      - observe(candle): called on every candle including cooldown bars.
        Update internal state here (indicators, rolling windows, EMAs).
        Default is no-op for stateless strategies.
      - decide(candle): called only on non-cooldown candles inside the
        engine's record window. Return a LendDecision or None.

    Param sweep (Phase 3b):
      - param_grid_for_cell(symbol, period_agg, eda): classmethod returning
        list of kwarg dicts to construct strategy variants for a given cell.
        Defaults to NotImplementedError; subclasses participating in the sweep
        must override.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    def observe(self, candle: FundingCandle) -> None:  # noqa: B027
        """Update internal state from candle. Default no-op."""

    @abstractmethod
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return list of kwarg dicts to instantiate variants for a cell.

        Subclasses override to participate in the Phase 3b sweep. The `eda`
        dict carries per-cell EDA outputs (sigmas, percentiles, effect sizes).
        """
        raise NotImplementedError(
            f"{cls.__name__} does not implement param_grid_for_cell"
        )
