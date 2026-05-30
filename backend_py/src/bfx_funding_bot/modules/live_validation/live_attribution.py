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

from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome

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


def _fill_duration_days(f: FillRecord) -> Decimal:
    """Held-to-term, capped by actual lifetime when a release exists."""
    if f.release_ts_ms is None:
        return f.period_days
    actual = Decimal(f.release_ts_ms - f.fill_ts_ms) / MS_PER_DAY
    if actual < 0:
        actual = Decimal("0")
    return min(f.period_days, actual)


def attribute_active(
    fills: list[FillRecord], *, capital: Decimal, window_bounds: list[tuple[int, int]]
) -> list[WindowOutcome]:
    """Strategy arm: realized lending interest normalized to the capital budget.

    A fill belongs to the window containing its fill_ts_ms. Per window:
    net_monthly = sum(size * rate * duration_days) / capital * 100.
    """
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        wf = [f for f in fills if lo <= f.fill_ts_ms < hi]
        interest = sum(
            (f.size_usdt * f.rate * _fill_duration_days(f) for f in wf), Decimal("0")
        )
        rates = [f.rate for f in wf]
        mean_rate = (
            sum(rates, Decimal("0")) / Decimal(len(rates)) if rates else Decimal("0")
        )
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=interest / capital * Decimal("100"),
                n_trades=len(wf),
                fill_rate=mean_rate,
            )
        )
    return out


def attribute_passive(
    frr_points: list[FrrPoint], *, window_bounds: list[tuple[int, int]]
) -> list[WindowOutcome]:
    """AlwaysFRR arm: full-budget lending at mean FRR over each window.

    net_monthly = mean(FRR in window) * window_days * 100  (capital cancels).
    """
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        pts = [p for p in frr_points if lo <= p.mts < hi]
        days = Decimal(hi - lo) / MS_PER_DAY
        mean_frr = (
            sum((p.frr for p in pts), Decimal("0")) / Decimal(len(pts))
            if pts
            else Decimal("0")
        )
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=mean_frr * days * Decimal("100"),
                n_trades=len(pts),
                fill_rate=mean_frr,
            )
        )
    return out


@dataclass(frozen=True)
class DeploymentAnchorResult:
    attributed_deployed: Decimal
    observed_realized: Decimal
    relative_divergence: Decimal
    within_tolerance: bool


def check_deployment_anchor(
    *, attributed_deployed: Decimal, observed_realized: Decimal, tol: Decimal
) -> DeploymentAnchorResult:
    """Relative divergence of attributed vs venue-observed deployed principal.

    When observed_realized == 0: divergence is 0 if attributed is also 0,
    else treated as fully divergent (infinite -> beyond any finite tolerance).
    """
    if observed_realized == 0:
        div = Decimal("0") if attributed_deployed == 0 else Decimal("Infinity")
    else:
        div = abs(attributed_deployed - observed_realized) / observed_realized
    return DeploymentAnchorResult(
        attributed_deployed=attributed_deployed,
        observed_realized=observed_realized,
        relative_divergence=div,
        within_tolerance=div <= tol,
    )
