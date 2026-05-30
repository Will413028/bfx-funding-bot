"""Live active-vs-passive attribution for the canary MeanReversion config.

Pure, I/O-free. Turns live fills + FRR series + reconcile checkpoints into
WindowOutcome lists (strategy arm + passive AlwaysFRR arm) feeding the existing
modules/backtest/oos_profitability metrics, plus a four-state verdict.

Both arms normalize to a fixed capital budget C (the canary allocation cap):
    active_return_pct  = sum(size_i * rate_i * duration_i) / C * 100
    passive_return_pct = mean(FRR over window) * window_days * 100   # C cancels
Idle drag is automatic: a strategy that deploys fewer capital-days than the
full-budget passive arm falls below it and the active spread goes negative.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MS_PER_DAY = Decimal(24 * 60 * 60 * 1000)


@dataclass(frozen=True)
class FillRecord:
    """One ORDER_FILL, joined with its RESERVATION_RELEASED (if any)."""

    venue_offer_id: str
    fill_ts_ms: int
    size_usdt: Decimal
    rate: Decimal  # daily funding rate at match (foc.rate)
    period_days: Decimal  # resolved by caller via cell_period_days
    release_ts_ms: int | None  # None if no release seen (assume held to term)


@dataclass(frozen=True)
class FrrPoint:
    """One funding_stats sample for the canary symbol."""

    mts: int
    frr: Decimal  # daily flash-return-rate
    avg_period: Decimal  # auto-period length in days


def cell_period_days(period_agg: str, frr_avg_period: Decimal) -> Decimal:
    """Held-to-term duration for a cell. p2 -> 2 days; a30 -> FRR auto-period."""
    if period_agg == "p2":
        return Decimal("2")
    if period_agg == "a30":
        return frr_avg_period
    raise ValueError(f"unknown period_agg: {period_agg!r}")


WEEK_MS = 7 * 24 * 60 * 60 * 1000


def weekly_window_bounds(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Calendar-week [lo, hi) bins covering [start_ms, end_ms).

    The trailing bin is truncated to end_ms. Empty if end_ms <= start_ms.
    """
    if end_ms <= start_ms:
        return []
    bounds: list[tuple[int, int]] = []
    lo = start_ms
    while lo < end_ms:
        hi = min(lo + WEEK_MS, end_ms)
        bounds.append((lo, hi))
        lo = hi
    return bounds
