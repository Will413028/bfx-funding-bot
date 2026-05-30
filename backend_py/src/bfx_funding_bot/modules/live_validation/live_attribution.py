"""Live active-vs-passive attribution for the canary MeanReversion config.

Pure, I/O-free. Turns live fills + market-rate series (funding_candles.close) +
reconcile checkpoints into WindowOutcome lists (strategy arm + passive
AlwaysMarketRate arm) feeding the existing modules/backtest/oos_profitability
metrics, plus a four-state verdict.

Both arms normalize to a fixed capital budget C (the canary allocation cap):
    active_return_pct  = sum(size_i * rate_i * duration_i) / C * 100
    passive_return_pct = mean(market_rate over window) * window_days * 100  # C cancels
The passive baseline is the per-day market funding rate (funding_candles.close),
matching the backtest AlwaysMarketRateStrategy. funding_stats.frr is NOT a
market-rate proxy (it is ~1e-6, ~185x too small); see assert_market_rate_band.
Idle drag is automatic: a strategy that deploys fewer capital-days than the
full-budget passive arm falls below it and the active spread goes negative.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import pairwise

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
class MarketRatePoint:
    """One funding_candles.close sample (per-day market funding rate) for the cell."""

    mts: int
    rate: Decimal  # per-day market funding rate (funding_candles close)


# Plausible per-day market funding-rate band for the canary cell. funding_candles
# .close lives around 1e-4..1e-3; funding_stats.frr (the old, wrong source) is
# ~1e-6 — ~185x smaller. This guard fails the G3 loader loudly if a future change
# feeds frr-scale (or percentage-scale) values into the passive baseline again.
_MIN_PLAUSIBLE_DAILY_RATE = Decimal("1e-5")
_MAX_PLAUSIBLE_DAILY_RATE = Decimal("0.05")


def assert_market_rate_band(rates: list[Decimal]) -> None:
    """Raise if the mean of `rates` falls outside the plausible per-day band.

    A no-op on an empty list (no coverage → nothing to assert; the loader's
    coverage guard handles that). Checks the mean rather than each point so a
    single legitimate funding spike does not trip it, while a systematic unit
    error (every point ~1e-6) does.
    """
    if not rates:
        return
    mean_rate = sum(rates, Decimal("0")) / Decimal(len(rates))
    if not (_MIN_PLAUSIBLE_DAILY_RATE <= mean_rate <= _MAX_PLAUSIBLE_DAILY_RATE):
        raise ValueError(
            f"market rate {mean_rate} outside plausible per-day band "
            f"[{_MIN_PLAUSIBLE_DAILY_RATE}, {_MAX_PLAUSIBLE_DAILY_RATE}] — "
            f"wrong data source? funding_stats.frr (~1e-6) is not a market rate; "
            f"use funding_candles.close"
        )


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


@dataclass(frozen=True)
class ClampedWindow:
    """Concurrency-clamped attribution over a set of fills (one bucket).

    interest / capital_days reflect the budget ceiling: at every instant the
    open principal is clamped to `cap` (all open fills scaled by cap/Σopen when
    over budget). raw_interest / peak_concurrent are the un-clamped figures kept
    for the over-deploy diagnostic. No window-end clipping — fills accrue their
    full held-to-term lifetime, so the no-clamp case is bit-exact with the
    legacy Σ(size·rate·duration).
    """

    interest: Decimal
    capital_days: Decimal
    raw_interest: Decimal
    peak_concurrent: Decimal


@dataclass(frozen=True)
class ClampDiagnostic:
    """Over-deploy transparency for the report.

    cap is the budget; peak_concurrent the max instantaneous open principal; raw
    vs clamped interest the excess the clamp removed.
    """

    cap: Decimal
    peak_concurrent: Decimal
    raw_interest: Decimal
    clamped_interest: Decimal

    @property
    def over_deployed(self) -> bool:
        return self.peak_concurrent > self.cap

    @property
    def over_deploy_factor(self) -> Decimal:
        return self.peak_concurrent / self.cap if self.cap > 0 else Decimal("0")

    @property
    def excess_return_pct(self) -> Decimal:
        # interest the clamp removed, as a % of budget (same unit as headline)
        if self.cap <= 0:
            return Decimal("0")
        return (self.raw_interest - self.clamped_interest) / self.cap * Decimal("100")


def clamp_active_window(fills: list[FillRecord], *, cap: Decimal) -> ClampedWindow:
    """Sweep-line attribution with concurrent-principal clamped to `cap`.

    Each fill occupies [fill_ts, fill_ts + _fill_duration_days·MS_PER_DAY).
    Per sub-interval: S = Σ open sizes; scale = min(1, cap/S). Durations are
    accumulated per fill in integer milliseconds (scale==1) so a non-clamped
    bucket divides by MS_PER_DAY exactly once → identical to the old formula.
    """
    if cap <= 0:
        raise ValueError(f"cap must be positive, got {cap!r}")
    intervals: list[tuple[int, int, FillRecord]] = []
    for f in fills:
        start = f.fill_ts_ms
        end = start + int(_fill_duration_days(f) * MS_PER_DAY)
        if end > start:
            intervals.append((start, end, f))
    if not intervals:
        return ClampedWindow(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))

    points = sorted({p for s, e, _ in intervals for p in (s, e)})
    scaled_ms = [Decimal("0")] * len(intervals)
    clipped_ms = [0] * len(intervals)
    peak = Decimal("0")
    for a, b in pairwise(points):
        dt = b - a
        if dt <= 0:
            continue
        open_idx = [i for i, (s, e, _) in enumerate(intervals) if s <= a < e]
        total_open = sum((intervals[i][2].size_usdt for i in open_idx), Decimal("0"))
        if total_open > peak:
            peak = total_open
        if total_open <= 0:
            continue
        scale = cap / total_open if total_open > cap else Decimal("1")
        for i in open_idx:
            scaled_ms[i] += scale * dt
            clipped_ms[i] += dt

    interest = Decimal("0")
    capital_days = Decimal("0")
    raw_interest = Decimal("0")
    for i, (_s, _e, f) in enumerate(intervals):
        sm = scaled_ms[i]
        cm = Decimal(clipped_ms[i])
        interest += f.size_usdt * f.rate * sm / MS_PER_DAY
        capital_days += f.size_usdt * sm / MS_PER_DAY
        raw_interest += f.size_usdt * f.rate * cm / MS_PER_DAY
    return ClampedWindow(
        interest=interest,
        capital_days=capital_days,
        raw_interest=raw_interest,
        peak_concurrent=peak,
    )


def attribute_active(
    fills: list[FillRecord], *, capital: Decimal, window_bounds: list[tuple[int, int]]
) -> list[WindowOutcome]:
    """Strategy arm: realized lending interest normalized to the capital budget.

    A fill belongs to the window containing its fill_ts_ms. Per window the
    bucket's realized interest is concurrency-clamped to the budget
    (clamp_active_window) so the active arm cannot "deploy" more than the cap the
    passive arm is normalized to: net_monthly = clamped_interest / capital * 100.
    """
    if capital <= 0:
        raise ValueError(f"capital must be positive, got {capital!r}")
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        wf = [f for f in fills if lo <= f.fill_ts_ms < hi]
        clamped = clamp_active_window(wf, cap=capital)
        rates = [f.rate for f in wf]
        # unweighted mean matched rate — diagnostic only, not used in the yield sum
        mean_rate = (
            sum(rates, Decimal("0")) / Decimal(len(rates)) if rates else Decimal("0")
        )
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=clamped.interest / capital * Decimal("100"),
                n_trades=len(wf),
                fill_rate=mean_rate,
            )
        )
    return out


def attribute_passive(
    points: list[MarketRatePoint], *, window_bounds: list[tuple[int, int]]
) -> list[WindowOutcome]:
    """AlwaysMarketRate arm: full-budget lending at mean market rate per window.

    net_monthly = mean(market_rate in window) * window_days * 100  (capital cancels).
    """
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        pts = [p for p in points if lo <= p.mts < hi]
        days = Decimal(hi - lo) / MS_PER_DAY
        mean_rate = (
            sum((p.rate for p in pts), Decimal("0")) / Decimal(len(pts))
            if pts
            else Decimal("0")
        )
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=mean_rate * days * Decimal("100"),
                n_trades=len(pts),
                fill_rate=mean_rate,
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
