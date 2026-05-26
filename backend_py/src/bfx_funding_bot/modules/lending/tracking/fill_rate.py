"""G13 fill-rate learning: path-crossing aggregation over funding candles.

See docs/superpowers/specs/2026-05-26-g13-fill-rate-learning-design.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field  # noqa: F401
from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle  # noqa: F401

# Spread buckets in basis points of relative spread (1% = 100 bps), non-uniform,
# denser near 0. Each value is a bucket midpoint; the learner evaluates a
# hypothetical offer placed at ref*(1 + bps/1e4).
BUCKET_GRID_BPS: list[int] = [-500, -200, -100, -50, 0, 50, 100, 200, 300, 500, 1000, 2000]

# Resting horizons (hours) over which fill is evaluated.
HORIZONS_H: list[int] = [1, 4, 24]

# Buckets with fewer samples than this are flagged low-confidence.
MIN_SAMPLES: int = 30

_MS_PER_HOUR = 3_600_000


@dataclass(frozen=True)
class BucketStat:
    """Learned fill stats for one (horizon, spread bucket) of one candle series."""

    horizon_h: int
    spread_bucket_bps: int
    fill_prob: Decimal
    n_samples: int
    ttf_p50_ms: int | None
    ttf_p90_ms: int | None
    mean_ttf_ms: int | None


def _percentile(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile of an ascending-sorted list. None if empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(q / 100 * len(sorted_values)))
    return sorted_values[rank - 1]
