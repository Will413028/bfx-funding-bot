"""G13 fill-rate learning: path-crossing aggregation over funding candles.

See docs/superpowers/specs/2026-05-26-g13-fill-rate-learning-design.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle

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


@dataclass
class _Acc:
    filled: int = 0
    total: int = 0
    ttfs: list[int] = field(default_factory=list)


class FillRateLearner:
    """Pure aggregator: candles → list[BucketStat]. No DB."""

    def __init__(
        self,
        *,
        bucket_grid: list[int] | None = None,
        horizons: list[int] | None = None,
    ) -> None:
        self.bucket_grid = list(bucket_grid) if bucket_grid is not None else list(BUCKET_GRID_BPS)
        self.horizons = list(horizons) if horizons is not None else list(HORIZONS_H)

    def learn(self, candles: list[FundingCandle]) -> list[BucketStat]:
        sorted_c = sorted(candles, key=lambda c: c.mts)
        acc: dict[tuple[int, int], _Acc] = {}

        for idx, c in enumerate(sorted_c):
            ref = c.close
            if ref is None or ref <= 0:
                continue
            decision_ms = c.mts + _MS_PER_HOUR  # the close is known when its candle ends
            for horizon_h in self.horizons:
                deadline = decision_ms + horizon_h * _MS_PER_HOUR
                # Only candles lying wholly inside (decision, deadline] count. A
                # candle straddling the deadline cannot say on which side its
                # trades happened, so that sample is unknown, not a fill or a miss.
                window: list[FundingCandle] = []
                j = idx + 1
                while j < len(sorted_c) and sorted_c[j].mts + _MS_PER_HOUR <= deadline:
                    if sorted_c[j].mts >= decision_ms:
                        window.append(sorted_c[j])
                    j += 1
                if not window:
                    continue
                for bps in self.bucket_grid:
                    offer = ref * (Decimal(1) + Decimal(bps) / Decimal(10000))
                    hit_mts: int | None = None
                    for w in window:
                        if w.high is not None and w.high >= offer:
                            hit_mts = w.mts
                            break
                    a = acc.setdefault((horizon_h, bps), _Acc())
                    a.total += 1
                    if hit_mts is not None:
                        a.filled += 1
                        # Time to fill is bounded by the hit candle's end, never by
                        # its start: the fill happened somewhere inside that hour.
                        a.ttfs.append(hit_mts + _MS_PER_HOUR - decision_ms)

        out: list[BucketStat] = []
        for (horizon_h, bps), a in sorted(acc.items()):
            fill_prob = Decimal(a.filled) / Decimal(a.total) if a.total else Decimal(0)
            ttfs_sorted = sorted(a.ttfs)
            out.append(BucketStat(
                horizon_h=horizon_h,
                spread_bucket_bps=bps,
                fill_prob=fill_prob,
                n_samples=a.total,
                ttf_p50_ms=_percentile(ttfs_sorted, 50.0),
                ttf_p90_ms=_percentile(ttfs_sorted, 90.0),
                mean_ttf_ms=(sum(a.ttfs) // len(a.ttfs)) if a.ttfs else None,
            ))
        return out
