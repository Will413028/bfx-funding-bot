"""G13 fill-rate query model: interpolates learned BucketStats."""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.lending.tracking.fill_rate import MIN_SAMPLES


@dataclass(frozen=True)
class FillEstimate:
    fill_prob: Decimal
    expected_ttf_ms: int | None
    n_samples: int
    low_confidence: bool


class _StatRow(Protocol):
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None


@dataclass(frozen=True)
class _Point:
    bps: int
    fill_prob: Decimal
    n_samples: int
    mean_ttf_ms: int | None


def _estimate_from(p: _Point) -> FillEstimate:
    return FillEstimate(
        fill_prob=p.fill_prob,
        expected_ttf_ms=p.mean_ttf_ms,
        n_samples=p.n_samples,
        low_confidence=p.n_samples < MIN_SAMPLES,
    )


def _interpolate(points: list[_Point], spread_bps: int) -> FillEstimate:
    """points sorted ascending by bps, non-empty."""
    if spread_bps <= points[0].bps:
        return _estimate_from(points[0])
    if spread_bps >= points[-1].bps:
        return _estimate_from(points[-1])
    for a, b in itertools.pairwise(points):
        if a.bps <= spread_bps <= b.bps:
            span = Decimal(b.bps - a.bps)
            frac = Decimal(spread_bps - a.bps) / span if span != 0 else Decimal(0)
            fp = a.fill_prob + (b.fill_prob - a.fill_prob) * frac
            fp = min(Decimal(1), max(Decimal(0), fp))
            nearer = a if frac < Decimal("0.5") else b
            n = min(a.n_samples, b.n_samples)
            return FillEstimate(
                fill_prob=fp,
                expected_ttf_ms=nearer.mean_ttf_ms,
                n_samples=n,
                low_confidence=n < MIN_SAMPLES,
            )
    return _estimate_from(points[-1])  # unreachable


class FillRateModel:
    def __init__(self, by_key: dict[tuple[str, int], list[_Point]]) -> None:
        self._by_key = by_key

    @classmethod
    def from_rows(cls, rows: list[_StatRow]) -> FillRateModel:
        by_key: dict[tuple[str, int], list[_Point]] = {}
        for r in rows:
            key = (r.period_agg, r.horizon_h)
            by_key.setdefault(key, []).append(_Point(
                bps=r.spread_bucket_bps,
                fill_prob=Decimal(str(r.fill_prob)),
                n_samples=r.n_samples,
                mean_ttf_ms=r.mean_ttf_ms,
            ))
        for pts in by_key.values():
            pts.sort(key=lambda p: p.bps)
        return cls(by_key)

    def estimate_fill(
        self,
        *,
        reference_rate: Decimal,
        offer_rate: Decimal,
        period_agg: str,
        horizon_h: int,
    ) -> FillEstimate | None:
        if reference_rate is None or reference_rate <= 0:
            raise ValueError(f"reference_rate must be > 0, got {reference_rate!r}")
        points = self._by_key.get((period_agg, horizon_h))
        if not points:
            return None
        spread_bps = round((offer_rate - reference_rate) / reference_rate * Decimal(10000))
        return _interpolate(points, spread_bps)
