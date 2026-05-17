"""Train/test split helper for Phase 3b sweep / eval flow.

Single source of truth for the 70/30 cut; consumed by both EDA script
and matrix runner so train_end_mts matches across the two flows.
"""
from __future__ import annotations

from bfx_funding_bot.modules.candles.schemas import FundingCandle

TRAIN_RATIO = 0.7


def compute_train_end_mts(candles: list[FundingCandle]) -> int:
    """Return the mts (inclusive) of the last candle in the train portion.

    Sorts candles by mts ascending then takes index `int(n * 0.7) - 1`,
    handling n=1 by returning the single candle's mts.
    """
    if not candles:
        raise ValueError("compute_train_end_mts: candles list is empty")
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    split_idx = int(len(sorted_candles) * TRAIN_RATIO)
    train_last_idx = max(0, split_idx - 1)
    return sorted_candles[train_last_idx].mts
