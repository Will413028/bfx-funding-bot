"""Execution-quality metrics — submit→first-fill latency per config regime.

Pure, I/O-free (loader = scripts/report_execution_quality.py). Weekly APR at
10k cap moves ~1.9 USDT/week per 1%/yr — invisible for months; latency and
fill-within-TTL respond to a pricing change (E2 clamp) within days.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    cid: int
    claimed_at_ms: int


@dataclass(frozen=True, slots=True)
class FillEvent:
    cid: int
    filled_at_ms: int


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    cid: int
    claimed_at_ms: int
    latency_ms: int | None  # None = never filled (as of the data snapshot)


@dataclass(frozen=True, slots=True)
class RegimeSummary:
    regime_start_ms: int
    n_claims: int
    n_filled: int
    fill_rate: Decimal
    p50_latency_ms: int | None
    p90_latency_ms: int | None
    n_unfilled_past_ttl: int


def pair_claims_to_fills(
    claims: list[ClaimEvent], fills: list[FillEvent],
) -> list[ClaimOutcome]:
    """First fill per cid wins (partial-fill streams share the cid)."""
    first_fill: dict[int, int] = {}
    for f in fills:
        cur = first_fill.get(f.cid)
        if cur is None or f.filled_at_ms < cur:
            first_fill[f.cid] = f.filled_at_ms
    return [
        ClaimOutcome(
            cid=c.cid,
            claimed_at_ms=c.claimed_at_ms,
            latency_ms=(
                first_fill[c.cid] - c.claimed_at_ms
                if c.cid in first_fill else None
            ),
        )
        for c in claims
    ]


def bucket_by_regime(
    outcomes: list[ClaimOutcome], regime_starts: list[int],
) -> dict[int, list[ClaimOutcome]]:
    """Assign each claim to the latest regime boot at-or-before it. Claims
    predating the first recorded regime are dropped (unknown flag state)."""
    starts = sorted(regime_starts)
    buckets: dict[int, list[ClaimOutcome]] = {s: [] for s in starts}
    for o in outcomes:
        i = bisect.bisect_right(starts, o.claimed_at_ms) - 1
        if i >= 0:
            buckets[starts[i]].append(o)
    return buckets


def _percentile(sorted_vals: list[int], pct: float) -> int:
    # ponytail: nearest-rank percentile — fine for operator tables,
    # swap for interpolation if this ever feeds a gate.
    idx = max(0, min(len(sorted_vals) - 1, round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def summarize(
    bucketed: dict[int, list[ClaimOutcome]], *, ttl_ms: int,
) -> list[RegimeSummary]:
    out: list[RegimeSummary] = []
    for start in sorted(bucketed):
        outcomes = bucketed[start]
        lats = sorted(o.latency_ms for o in outcomes if o.latency_ms is not None)
        out.append(RegimeSummary(
            regime_start_ms=start,
            n_claims=len(outcomes),
            n_filled=len(lats),
            fill_rate=(
                Decimal(len(lats)) / Decimal(len(outcomes))
                if outcomes else Decimal("0")
            ),
            p50_latency_ms=_percentile(lats, 0.5) if lats else None,
            p90_latency_ms=_percentile(lats, 0.9) if lats else None,
            n_unfilled_past_ttl=sum(
                1 for o in outcomes
                if o.latency_ms is None or o.latency_ms > ttl_ms
            ),
        ))
    return out
