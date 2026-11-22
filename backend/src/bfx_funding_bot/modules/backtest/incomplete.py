"""Fail-closed signal for research backtests that lack the evidence they need."""
from typing import Literal

BacktestIncompleteReason = Literal[
    "fill_model_missing", "fill_model_low_confidence", "fill_model_scope_mismatch",
    "market_series_gap", "tape_day_missing",
]


class BacktestIncomplete(RuntimeError):  # noqa: N818 - contract name is prescribed
    """A requested backtest lacks usable fill evidence (model, market series or tape day)."""

    def __init__(self, reason: BacktestIncompleteReason) -> None:
        super().__init__(reason)
        self.reason = reason
