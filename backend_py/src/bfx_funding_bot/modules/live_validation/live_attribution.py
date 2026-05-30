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
from enum import Enum

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
    avg_period: Decimal  # auto-period length in days; used by the loader for cell_period_days, NOT by attribute_passive


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


def open_principal_at(fills: list[FillRecord], as_of_ms: int) -> Decimal:
    """Total principal still lent at `as_of_ms` (held-to-term, release-aware).

    A fill is open at `as_of_ms` if it was filled at/before then and its effective
    end (release time if released, else fill_ts + period) is strictly after then.
    This is directly comparable to a point-in-time venue realized-principal snapshot,
    unlike a time-averaged deployed figure.
    """
    total = Decimal("0")
    for f in fills:
        if f.fill_ts_ms > as_of_ms:
            continue
        if f.release_ts_ms is not None:
            effective_end = f.release_ts_ms
        else:
            effective_end = f.fill_ts_ms + int(f.period_days * MS_PER_DAY)
        if effective_end > as_of_ms:
            total += f.size_usdt
    return total


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
    if capital <= 0:
        raise ValueError(f"capital must be positive, got {capital!r}")
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        wf = [f for f in fills if lo <= f.fill_ts_ms < hi]
        interest = sum(
            (f.size_usdt * f.rate * _fill_duration_days(f) for f in wf), Decimal("0")
        )
        rates = [f.rate for f in wf]
        # unweighted mean matched rate — diagnostic only, not used in the yield sum
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


@dataclass(frozen=True)
class NavAnchorResult:
    available: bool
    nav_delta: Decimal | None
    attributed_interest: Decimal
    relative_divergence: Decimal | None
    within_tolerance: bool


def check_nav_anchor(
    *, nav_delta: Decimal | None, attributed_interest: Decimal, tol: Decimal
) -> NavAnchorResult:
    """Best-effort ΔNAV vs Σ attributed interest. Unavailable -> within_tolerance True.

    When attributed_interest == 0: divergence is 0 if nav_delta is also 0, else treated as fully divergent.
    """
    if nav_delta is None:
        return NavAnchorResult(
            available=False,
            nav_delta=None,
            attributed_interest=attributed_interest,
            relative_divergence=None,
            within_tolerance=True,
        )
    if attributed_interest == 0:
        div = Decimal("0") if nav_delta == 0 else Decimal("Infinity")
    else:
        div = abs(nav_delta - attributed_interest) / abs(attributed_interest)
    return NavAnchorResult(
        available=True,
        nav_delta=nav_delta,
        attributed_interest=attributed_interest,
        relative_divergence=div,
        within_tolerance=div <= tol,
    )


class VerdictState(Enum):
    PASS = "PASS"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    FAIL = "FAIL"
    UNRELIABLE = "UNRELIABLE"


@dataclass(frozen=True)
class G3Verdict:
    state: VerdictState
    headline_active_spread: Decimal
    n_windows: int
    ci_lo: Decimal
    ci_hi: Decimal
    reasons: list[str]


def decide_verdict(
    *,
    headline_active_spread: Decimal,
    n_windows: int,
    total_capital_days: Decimal,
    ci_lo: Decimal,
    ci_hi: Decimal,
    deployment_anchor: DeploymentAnchorResult,
    nav_anchor: NavAnchorResult,
    min_windows: int,
    min_capital_days: Decimal,
) -> G3Verdict:
    """Pure four-state decision table. See plan Task 7 for evaluation order."""
    reasons: list[str] = []

    def verdict(state: VerdictState) -> G3Verdict:
        return G3Verdict(
            state=state,
            headline_active_spread=headline_active_spread,
            n_windows=n_windows,
            ci_lo=ci_lo,
            ci_hi=ci_hi,
            reasons=reasons,
        )

    if not deployment_anchor.within_tolerance:
        reasons.append(
            f"deployment anchor diverged: attributed {deployment_anchor.attributed_deployed} "
            f"vs observed {deployment_anchor.observed_realized}"
        )
        return verdict(VerdictState.UNRELIABLE)
    if not nav_anchor.within_tolerance:
        reasons.append(
            f"NAV anchor diverged: ΔNAV {nav_anchor.nav_delta} "
            f"vs attributed interest {nav_anchor.attributed_interest}"
        )
        return verdict(VerdictState.UNRELIABLE)

    if n_windows < min_windows:
        reasons.append(f"only {n_windows} weekly windows (need >= {min_windows})")
        return verdict(VerdictState.INSUFFICIENT_DATA)
    if total_capital_days < min_capital_days:
        reasons.append(
            f"deployed {total_capital_days} capital-days (need >= {min_capital_days})"
        )
        return verdict(VerdictState.INSUFFICIENT_DATA)

    if ci_hi < 0:
        reasons.append(f"active-spread CI [{ci_lo}, {ci_hi}] entirely below 0")
        return verdict(VerdictState.FAIL)
    if ci_lo > 0:
        reasons.append(f"active-spread CI [{ci_lo}, {ci_hi}] entirely above 0")
        return verdict(VerdictState.PASS)

    reasons.append(f"active-spread CI [{ci_lo}, {ci_hi}] straddles 0 — inconclusive")
    return verdict(VerdictState.INSUFFICIENT_DATA)
