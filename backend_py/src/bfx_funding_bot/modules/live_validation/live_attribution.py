"""Live bot-vs-idle attribution for the canary MeanReversion config.

Pure, I/O-free. Turns live fills + market-rate series (funding_candles.close) +
reconcile checkpoints into WindowOutcome lists for three arms — strategy
(active), AlwaysIdle, AlwaysMarketRate — feeding the existing
modules/backtest/oos_profitability metrics, plus a four-state verdict.

All arms normalize to a fixed capital budget C (the canary allocation cap):
    active_return_pct  = sum(size_i * rate_i * duration_i) / C * 100  # idle drag baked in
    idle_return_pct    = 0                                            # by construction
    passive_return_pct = mean(market_rate over window) * window_days * 100  # C cancels

PRIMARY gate (the product's success criterion): bot-vs-idle = active − idle. Since
idle ≡ 0, this equals the active arm's absolute return on budget — "does the bot
earn the market rate on the user's capital vs leaving it idle?". Idle drag is
automatic: active_return_pct already counts undeployed capital as earning 0.

SECONDARY diagnostic (reported, NEVER gating): MR alpha = active − AlwaysMarketRate.
The passive baseline is the per-day market funding rate (funding_candles.close),
matching the backtest AlwaysMarketRateStrategy. funding_stats.frr is NOT a
market-rate proxy (it is ~1e-6, ~185x too small); see assert_market_rate_band. A
wrong-scale or absent passive series only marks the MR-alpha diagnostic
unavailable — it does not affect the bot-vs-idle verdict (idle needs no market data).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import pairwise

from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

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


# ---- E3: AlwaysFRR benchmark arm（docs/research/2026-07-06-profit-design-review.md §1 E3 (c)）----
# funding_stats.frr 不是市場利率、也非 candle close 的單位轉換（ADR
# 2026-05-28-frr-not-a-market-rate-proxy；5 個假設全 FAIL）。但它是 ticker FRR
# 的 /365 表示：2026-07-06 兩 symbol 實測 frr×365 ≈ ticker FRR（per-day）誤差
# <0.5%（plan 2026-07-06-e3-measurement-automation.md 背景段）。AlwaysFRR arm
# 用 frr×365 當「FRR auto-renew 掛單者實得的日利率」序列；換算後仍須過
# assert_market_rate_band（雙保險：任何未來單位漂移會炸 loader 而非產出錯報告）。
FRR_ANNUALIZATION = Decimal("365")


def frr_points_from_stats(stats: list[FundingStat]) -> list[MarketRatePoint]:
    """funding_stats rows → AlwaysFRR arm 的 per-day rate 序列（×365，skip null）。"""
    return [
        MarketRatePoint(mts=s.mts, rate=s.frr * FRR_ANNUALIZATION)
        for s in stats
        if s.frr is not None
    ]


@dataclass(frozen=True)
class FrrBenchmark:
    """bot(active) vs AlwaysFRR 的比較結果 — 報告/JSON 用，NEVER 改變 verdict 狀態。

    這是 cap 加碼的政策 gating bar（「贏不了免費的 FRR auto-renew 就是零附加值」），
    由 operator 讀報告執行，不進 decide_verdict 狀態機。
    """

    available: bool
    spread: Decimal   # active − AlwaysFRR 的 paired headline spread（%，gross）
    ci_lo: Decimal
    ci_hi: Decimal
    reason: str | None  # unavailable 時的人話原因；available 時 None


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


def fill_duration_days(f: FillRecord) -> Decimal:
    """Held-to-term, capped by actual lifetime when a release exists.

    （E3 公開化：weekly_attribution 需要同一套 duration 語意。）"""
    if f.release_ts_ms is None:
        return f.period_days
    actual = Decimal(f.release_ts_ms - f.fill_ts_ms) / MS_PER_DAY
    if actual < 0:
        actual = Decimal("0")
    return min(f.period_days, actual)


_fill_duration_days = fill_duration_days  # 舊名 alias（防漏改；勿新增使用）


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

    Each fill occupies [fill_ts, fill_ts + fill_duration_days·MS_PER_DAY).
    Per sub-interval: S = Σ open sizes; scale = min(1, cap/S). Durations are
    accumulated per fill in integer milliseconds (scale==1) so a non-clamped
    bucket divides by MS_PER_DAY exactly once → identical to the old formula.
    """
    if cap <= 0:
        raise ValueError(f"cap must be positive, got {cap!r}")
    intervals: list[tuple[int, int, FillRecord]] = []
    for f in fills:
        start = f.fill_ts_ms
        end = start + int(fill_duration_days(f) * MS_PER_DAY)
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


def attribute_idle(*, window_bounds: list[tuple[int, int]]) -> list[WindowOutcome]:
    """AlwaysIdle arm: capital sits idle, earning 0 by construction.

    One zero WindowOutcome per window. The bot-vs-idle paired difference
    (attribute_active − attribute_idle) therefore equals the active arm's
    absolute per-window return — the product's primary success metric (earn the
    market rate vs leave the balance idle). Mirrors attribute_passive's shape so
    paired_active_returns aligns the two arms 1:1 by month_mts.
    """
    return [
        WindowOutcome(
            month_mts=lo,
            net_monthly=Decimal("0"),
            n_trades=0,
            fill_rate=Decimal("0"),
        )
        for lo, _hi in window_bounds
    ]


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
    headline_bot_vs_idle: Decimal  # absolute active return on budget; bot-vs-idle (idle ≡ 0)
    n_windows: int
    ci_lo: Decimal  # bot-vs-idle 95% CI lower bound (primary gate)
    ci_hi: Decimal  # upper bound
    reasons: list[str]
    # Secondary MR-timing-alpha diagnostic (active − AlwaysMarketRate). Reported,
    # never gating. mr_alpha_available is False when market-rate coverage/band
    # makes the passive arm untrustworthy → render as "unavailable".
    mr_alpha_spread: Decimal
    mr_alpha_ci_lo: Decimal
    mr_alpha_ci_hi: Decimal
    mr_alpha_available: bool


def decide_verdict(
    *,
    headline_bot_vs_idle: Decimal,
    n_windows: int,
    total_capital_days: Decimal,
    ci_lo: Decimal,
    ci_hi: Decimal,
    deployment_anchor: DeploymentAnchorResult,
    nav_anchor: NavAnchorResult,
    min_windows: int,
    min_capital_days: Decimal,
    mr_alpha_spread: Decimal,
    mr_alpha_ci_lo: Decimal,
    mr_alpha_ci_hi: Decimal,
    mr_alpha_available: bool,
) -> G3Verdict:
    """Pure four-state decision. Primary gate = bot-vs-idle CI (ci_lo/ci_hi).

    MR-alpha fields are stamped onto the result for the report but never change
    the state — the product's success criterion is absolute return vs idle, not
    timing alpha vs AlwaysMarketRate. Anchor divergence (attribution-vs-venue
    truth) still forces UNRELIABLE regardless of the primary metric.
    """
    reasons: list[str] = []

    def verdict(state: VerdictState) -> G3Verdict:
        return G3Verdict(
            state=state,
            headline_bot_vs_idle=headline_bot_vs_idle,
            n_windows=n_windows,
            ci_lo=ci_lo,
            ci_hi=ci_hi,
            reasons=reasons,
            mr_alpha_spread=mr_alpha_spread,
            mr_alpha_ci_lo=mr_alpha_ci_lo,
            mr_alpha_ci_hi=mr_alpha_ci_hi,
            mr_alpha_available=mr_alpha_available,
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
        reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] entirely below 0")
        return verdict(VerdictState.FAIL)
    if ci_lo > 0:
        reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] entirely above 0")
        return verdict(VerdictState.PASS)

    reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] straddles 0 — inconclusive")
    return verdict(VerdictState.INSUFFICIENT_DATA)
