"""Train/test split helper for Phase 3b sweep / eval flow.

Single source of truth for the 70/30 cut; consumed by both EDA script
and matrix runner so train_end_mts matches across the two flows.
"""
from __future__ import annotations

from bfx_funding_bot.modules.candles.schemas import FundingCandle

TRAIN_RATIO = 0.7


def compute_train_end_mts(candles: list[FundingCandle]) -> int:
    """Return the mts (inclusive) of the last candle in the train portion.

    Computes split_idx = int(n * 0.7) and returns sorted_candles[max(0, split_idx - 1)].mts.
    Sorts input by mts ascending first.

    Caller contract: this function is permissive and will return a valid mts for any non-empty
    input. For n < 2, the "train portion" degenerates to the entire input (test portion is empty).
    **Callers are responsible for validating that the input has enough candles for a meaningful
    70/30 split** (e.g., Phase 3b EDA + matrix runner both enforce len(candles) >= 720 = 1 month
    of 1h candles per cell before invoking this helper).
    """
    if not candles:
        raise ValueError("compute_train_end_mts: candles list is empty")
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    split_idx = int(len(sorted_candles) * TRAIN_RATIO)
    train_last_idx = max(0, split_idx - 1)
    return sorted_candles[train_last_idx].mts
