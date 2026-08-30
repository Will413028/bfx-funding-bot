"""G13 fill-rate query model: interpolates learned BucketStats."""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from bfx_funding_bot.modules.lending.tracking.artifact import (
    FillModelArtifact,
    FillModelEvidence,
    FillModelUnavailable,
)


@dataclass(frozen=True)
class _StatRow(Protocol):
    source: str
    symbol: str
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None
    artifact_hash: str | None


@dataclass(frozen=True)
class _Point:
    bps: int
    fill_prob: Decimal
    n_samples: int
    mean_ttf_ms: int | None


def _interpolate(points: list[_Point], spread_bps: int) -> _Point:
    """points sorted ascending by bps, non-empty."""
    if spread_bps <= points[0].bps:
        return points[0]
    if spread_bps >= points[-1].bps:
        return points[-1]
    for a, b in itertools.pairwise(points):
        if a.bps <= spread_bps <= b.bps:
            span = Decimal(b.bps - a.bps)
            frac = Decimal(spread_bps - a.bps) / span if span != 0 else Decimal(0)
            fp = a.fill_prob + (b.fill_prob - a.fill_prob) * frac
            fp = min(Decimal(1), max(Decimal(0), fp))
            nearer = a if frac < Decimal("0.5") else b
            return _Point(
                bps=spread_bps,
                fill_prob=fp,
                n_samples=min(a.n_samples, b.n_samples),
                mean_ttf_ms=nearer.mean_ttf_ms,
            )
    return points[-1]  # unreachable


class FillRateModel:
    def __init__(
        self,
        by_key: dict[tuple[str, int], list[_Point]],
        artifact: FillModelArtifact | None,
        unavailable_reason: Literal[
            "missing", "low_confidence", "scope_mismatch", "unversioned"
        ] | None = None,
    ) -> None:
        self._by_key = by_key
        self._artifact = artifact
        self._unavailable_reason = unavailable_reason

    @property
    def unavailable_reason(self) -> Literal[
        "missing", "low_confidence", "scope_mismatch", "unversioned"
    ] | None:
        """Return a typed readiness failure, including empty evidence models."""
        if self._unavailable_reason is not None:
            return self._unavailable_reason
        if not self._by_key:
            return "missing"
        return None

    @classmethod
    def from_rows(
        cls,
        rows: list[_StatRow],
        *,
        artifact: FillModelArtifact | None,
    ) -> FillRateModel:
        if artifact is None:
            return cls({}, None, "unversioned")
        if any(row.artifact_hash is None for row in rows):
            return cls({}, artifact, "unversioned")
        if any(row.artifact_hash != artifact.artifact_hash for row in rows):
            return cls({}, artifact, "scope_mismatch")
        if any(
            row.source != artifact.source
            or row.symbol != artifact.symbol
            or row.period_agg != artifact.period_agg
            or row.horizon_h != artifact.horizon_h
            for row in rows
        ):
            return cls({}, artifact, "scope_mismatch")
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
        return cls(by_key, artifact)

    def estimate_fill(
        self,
        *,
        reference_rate: Decimal,
        offer_rate: Decimal,
        period_agg: str,
        horizon_h: int,
    ) -> FillModelEvidence | FillModelUnavailable:
        if reference_rate is None or reference_rate <= 0:
            raise ValueError(f"reference_rate must be > 0, got {reference_rate!r}")
        if self.unavailable_reason is not None:
            return FillModelUnavailable(self.unavailable_reason)
        if self._artifact is None:
            return FillModelUnavailable("unversioned")
        if (period_agg, horizon_h) != (
            self._artifact.period_agg,
            self._artifact.horizon_h,
        ):
            return FillModelUnavailable("scope_mismatch")
        points = self._by_key.get((period_agg, horizon_h))
        if not points:
            return FillModelUnavailable("missing")
        spread_bps = round((offer_rate - reference_rate) / reference_rate * Decimal(10000))
        point = _interpolate(points, spread_bps)
        if point.n_samples < self._artifact.confidence_min_samples:
            return FillModelUnavailable("low_confidence")
        return FillModelEvidence(
            fill_prob=point.fill_prob,
            expected_ttf_ms=point.mean_ttf_ms,
            n_samples=point.n_samples,
            symbol=self._artifact.symbol,
            period_agg=self._artifact.period_agg,
            horizon_h=self._artifact.horizon_h,
            model_version=self._artifact.model_version,
            artifact_hash=self._artifact.artifact_hash,
            cutoff_ms=self._artifact.cutoff_ms,
        )

    @property
    def artifact(self) -> FillModelArtifact | None:
        return self._artifact
