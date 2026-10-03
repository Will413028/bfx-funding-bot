"""Book-replay fill learning (Proposal C, 2026-09-22 review §5) — pure, no DB.

The G13 candle learner asks "did the price path cross my offer?"; it cannot see
the queue in front of an offer, so any quote at or below the last print fills
with probability 1. This learner replays the self-recorded funding book
(`funding_book_snapshots`, top-25 P0 levels since 2026-07-19) instead:

  for a snapshot at time t and a hypothetical offer at ref × (1 + bps/1e4) for
  `period_days`, queue_ahead = Σ ask amount at that period with rate ≤ offer
  (same-rate FIFO counts as ahead); the offer fills within horizon H once the
  cumulative traded volume of the candles strictly after t, up to t+H, reaches
  queue_ahead + offer_amount.

`ref` is the close of the last *completed* hour before t (`ref_lag_hours`), so
the spread axis matches what the engine passes as `reference_rate` and no
in-progress candle leaks into the reference. Volume is the funding candle's
`volume` for the same period series (p2 for period 2). Only candles lying
wholly inside (t, t+H] count towards horizon H; with hourly candles a 1h
horizon is therefore resolvable only for snapshots taken on the hour, and an
unresolvable horizon yields no sample rather than a miss.

Known optimism: newcomers who undercut after t are not modelled, and traded
volume is assumed to consume the book from the best ask upward. Calibrate
against live submit→fill latencies before trusting absolute levels (plan C4).
Output is the same `BucketStat` shape as G13 so the artifact / model / storage
machinery is shared; the artifact carries `source="book"` and is never mixed
with candle artifacts (tech page Case 11).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.fill_rate import (
    BUCKET_GRID_BPS,
    HORIZONS_H,
    BucketStat,
    _percentile,
)
from bfx_funding_bot.modules.lending.tracking.queue_fill import filled_amount, queue_ahead_of
from bfx_funding_bot.modules.marketfeed.book_period_coverage import BookAskSnapshot

BOOK_SOURCE = "book"
BOOK_MODEL_VERSION = "book-replay-v2"  # v2: horizon counts only candles wholly inside the window
DEFAULT_OFFER_AMOUNT = Decimal("150")  # venue minimum; queue math is depth-relative
_MS_PER_HOUR = 3_600_000


@dataclass
class _Acc:
    filled: int = 0
    total: int = 0
    ttfs: list[int] = field(default_factory=list)


def queue_ahead(snapshot: BookAskSnapshot, *, period_days: int, offer_rate: Decimal) -> Decimal:
    """Ask amount at `period_days` resting at or below `offer_rate` (FIFO: same rate counts)."""
    return queue_ahead_of(snapshot.asks, period_days=period_days, offer_rate=offer_rate)


class BookReplayLearner:
    """Pure aggregator: (snapshots, candles) → list[BucketStat]."""

    def __init__(
        self,
        *,
        period_days: int = 2,
        offer_amount: Decimal = DEFAULT_OFFER_AMOUNT,
        bucket_grid: Sequence[int] | None = None,
        horizons: Sequence[int] | None = None,
        ref_lag_hours: int = 1,
    ) -> None:
        if period_days < 2 or offer_amount <= 0 or ref_lag_hours < 1:
            raise ValueError("period_days >= 2, offer_amount > 0, ref_lag_hours >= 1 required")
        self.period_days = period_days
        self.offer_amount = offer_amount
        self.bucket_grid = list(bucket_grid) if bucket_grid is not None else list(BUCKET_GRID_BPS)
        self.horizons = list(horizons) if horizons is not None else list(HORIZONS_H)
        self.ref_lag_hours = ref_lag_hours

    @property
    def period_agg(self) -> str:
        """The candle series key this model prices: `p2` for 2-day levels, `p30` for 30-day."""
        return f"p{self.period_days}"

    def learn(
        self, snapshots: Sequence[BookAskSnapshot], candles: Sequence[FundingCandle]
    ) -> list[BucketStat]:
        by_mts = {c.mts: c for c in candles}
        ordered = sorted(candles, key=lambda c: c.mts)
        acc: dict[tuple[int, int], _Acc] = {}

        for snap in sorted(snapshots, key=lambda s: s.captured_at_ms):
            t = snap.captured_at_ms
            hour_start = t - t % _MS_PER_HOUR
            ref_candle = by_mts.get(hour_start - self.ref_lag_hours * _MS_PER_HOUR)
            if ref_candle is None or ref_candle.close is None or ref_candle.close <= 0:
                continue
            ref = ref_candle.close
            max_horizon_ms = max(self.horizons) * _MS_PER_HOUR
            # Only candles that start at or after the snapshot and end within the
            # longest horizon; per-horizon membership is re-checked below.
            window = [c for c in ordered if c.mts >= t and c.mts + _MS_PER_HOUR <= t + max_horizon_ms]
            if not window:
                continue
            for bps in self.bucket_grid:
                offer = ref * (Decimal(1) + Decimal(bps) / Decimal(10000))
                ahead = queue_ahead(snap, period_days=self.period_days, offer_rate=offer)
                cumulative = Decimal("0")
                fill_at_ms: int | None = None
                for c in window:
                    cumulative += c.volume if c.volume is not None else Decimal("0")
                    if filled_amount(
                        queue_ahead=ahead, amount=self.offer_amount, cumulative_volume=cumulative,
                    ) >= self.offer_amount:
                        fill_at_ms = c.mts + _MS_PER_HOUR  # filled somewhere inside that hour
                        break
                for horizon_h in self.horizons:
                    deadline = t + horizon_h * _MS_PER_HOUR
                    if not any(c.mts + _MS_PER_HOUR <= deadline for c in window):
                        continue  # no candle resolvable inside this horizon: unknown, not a sample
                    a = acc.setdefault((horizon_h, bps), _Acc())
                    a.total += 1
                    if fill_at_ms is not None and fill_at_ms <= deadline:
                        a.filled += 1
                        a.ttfs.append(fill_at_ms - t)

        out: list[BucketStat] = []
        for (horizon_h, bps), a in sorted(acc.items()):
            ttfs_sorted = sorted(a.ttfs)
            out.append(BucketStat(
                horizon_h=horizon_h,
                spread_bucket_bps=bps,
                fill_prob=Decimal(a.filled) / Decimal(a.total) if a.total else Decimal(0),
                n_samples=a.total,
                ttf_p50_ms=_percentile(ttfs_sorted, 50.0),
                ttf_p90_ms=_percentile(ttfs_sorted, 90.0),
                mean_ttf_ms=(sum(a.ttfs) // len(a.ttfs)) if a.ttfs else None,
            ))
        return out
