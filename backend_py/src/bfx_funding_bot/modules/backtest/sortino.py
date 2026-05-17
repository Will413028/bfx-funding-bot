"""Sortino ratio on month-end equity curve sampling.

Per Phase 3b spec — Sortino is computed from monthly equity-curve
returns (sampled at month-end timestamps), not from per-trade returns.
This makes the metric independent of trade frequency and comparable
across strategies.

Edge cases:
- len(monthly_returns) < 3      -> 0     (sample too small to trust)
- len(downside_returns) == 0    -> +inf  (no downside observed; will be
                                          tie-broken by raw return in sweep)
- downside std == 0 with >=1 obs -> +inf  (degenerate, treated same as above)
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise


def monthly_returns_from_equity_curve(
    curve: list[tuple[int, Decimal]],
) -> list[Decimal]:
    """Compute month-over-month returns from an equity curve.

    Args:
        curve: list of (mts_ms, equity) tuples sampled at month-end timestamps,
               sorted ascending by mts.

    Returns:
        List of returns r_i = equity[i+1] / equity[i] - 1 for adjacent pairs.
        Empty list if fewer than 2 points.
    """
    if len(curve) < 2:
        return []
    sorted_curve = sorted(curve, key=lambda p: p[0])
    out: list[Decimal] = []
    for prev, cur in pairwise(sorted_curve):
        if prev[1] == 0:
            out.append(Decimal("0"))
        else:
            out.append(cur[1] / prev[1] - Decimal("1"))
    return out


def compute_sortino(returns: list[Decimal]) -> Decimal:
    """Sortino ratio: mean(returns) / std(downside_returns).

    Per spec edge-case ladder:
    - len(returns) < 3            -> 0       (untrustworthy sample)
    - no downside obs OR std==0   -> +inf    (sweep tie-broken by raw return)
    """
    if len(returns) < 3:
        return Decimal("0")
    n = Decimal(len(returns))
    mean = sum(returns, Decimal("0")) / n
    downside = [r for r in returns if r < 0]
    if not downside:
        return Decimal("Infinity")
    dn = Decimal(len(downside))
    dmean = sum(downside, Decimal("0")) / dn
    # population variance (denominator = N), not sample (N-1); matches numpy.std default
    var = sum((r - dmean) * (r - dmean) for r in downside) / dn
    if var == 0:
        return Decimal("Infinity")
    std = var.sqrt()
    return mean / std


def month_end_timestamps_within(
    start_mts: int, end_mts: int
) -> list[int]:
    """Return month-end UTC timestamps (mts in ms) within [start_mts, end_mts].

    Month-end = last second of last day of each month at 23:59:59 UTC.
    Used to sample equity curve from engine.
    """
    if start_mts > end_mts:
        return []
    out: list[int] = []
    start_dt = datetime.fromtimestamp(start_mts / 1000, UTC)
    year, month = start_dt.year, start_dt.month
    while True:
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        first_of_next = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=UTC)
        last_second = int(first_of_next.timestamp() * 1000) - 1000  # subtract 1s
        if last_second > end_mts:
            break
        if last_second >= start_mts:
            out.append(last_second)
        year, month = next_year, next_month
    return out
