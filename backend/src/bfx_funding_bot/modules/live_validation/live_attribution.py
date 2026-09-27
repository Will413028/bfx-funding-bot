"""Live bot-vs-idle attribution for the canary MeanReversion config.

Pure, I/O-free. Turns the bot's venue credits + market-rate series
(funding_candles.close) into WindowOutcome lists for three arms — strategy
(active), AlwaysIdle, AlwaysMarketRate — feeding the existing
modules/backtest/oos_profitability metrics, plus a four-state verdict.

Credit model (since 2026-09-27, METHODOLOGY_CHANGE_DATE). The active arm is the
interest the venue credits actually accrued: amount x credit rate x the time
each credit was held (MTS_OPENING..MTS_LAST_PAYOUT, or ..now while open),
clipped to each window, for the credits that funding_trades attribute to one of
the bot's cells (credit_attribution). It replaced ORDER_FILL size x fill rate x
held-to-term days, which booked a credit repaid after 14 minutes as 2 days.

All arms normalize to a capital budget C per window (the funding-wallet balance
the venue ledger reports, or an explicit --capital):
    active_return_pct  = sum(amount_i * rate_i * held_days_i in window) / C * 100
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

Trust gate: the weekly ledger reconciliation (credit net interest vs the venue's
interest payouts, credit_attribution.reconcile_week). A flagged complete week
means the credit model disagrees with what the venue paid → UNRELIABLE.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

if TYPE_CHECKING:
    # Annotations only: credit_attribution -> weekly_attribution imports this module.
    from bfx_funding_bot.modules.live_validation.credit_attribution import (
        CreditLifetime,
        WeeklyReconciliation,
    )

MS_PER_DAY = Decimal(24 * 60 * 60 * 1000)

# The G3 report switched from ORDER_FILL x held-to-term with a fixed allocation
# cap as C to the venue-credit model with the ledger wallet balance as C.
# Reports produced before this date are not comparable with later ones.
METHODOLOGY_CHANGE_DATE = "2026-09-27"


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


# The capital budget C: a fixed amount, or C per window [lo, hi) (e.g. the
# funding-wallet balance the venue ledger reports for that window).
CapitalBasis = Decimal | Callable[[int, int], Decimal]


def capital_for(capital: CapitalBasis, lo: int, hi: int) -> Decimal:
    """C for window [lo, hi); raises unless positive."""
    c = capital(lo, hi) if callable(capital) else capital
    if c <= 0:
        raise ValueError(f"capital must be positive, got {c!r}")
    return c

def credit_capital_days(
    credits: Sequence[CreditLifetime], lo: int, hi: int, *, now_ms: int
) -> Decimal:
    """Principal x days the credits were lent inside [lo, hi)."""
    return sum(
        (c.amount * Decimal(c.held_ms(lo, hi, now_ms=now_ms)) / MS_PER_DAY for c in credits),
        Decimal("0"),
    )


def credit_gross_interest(
    credits: Sequence[CreditLifetime], lo: int, hi: int, *, now_ms: int
) -> Decimal:
    """Gross (pre-fee) interest the credits accrued inside [lo, hi)."""
    return sum((c.gross_interest(lo, hi, now_ms=now_ms) for c in credits), Decimal("0"))


def attribute_active(
    credits: Sequence[CreditLifetime],
    *,
    capital: CapitalBasis,
    window_bounds: list[tuple[int, int]],
    now_ms: int,
) -> list[WindowOutcome]:
    """Strategy arm: interest the bot's credits accrued, on the capital budget.

    Each credit contributes amount x rate x the part of its held time that
    falls inside the window, so a credit spanning a window boundary is split
    rather than booked whole to the window it opened in, and one repaid early
    earns only the time it was lent. net_monthly = gross interest / C * 100
    (gross, like the passive arms; the report applies the fee separately).
    n_trades counts credits held inside the window; fill_rate is their
    capital-weighted mean rate (diagnostic only).
    """
    if not callable(capital):
        capital_for(capital, 0, 0)
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        window_capital = capital_for(capital, lo, hi)
        interest = credit_gross_interest(credits, lo, hi, now_ms=now_ms)
        capital_days = credit_capital_days(credits, lo, hi, now_ms=now_ms)
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=interest / window_capital * Decimal("100"),
                n_trades=sum(1 for c in credits if c.held_ms(lo, hi, now_ms=now_ms) > 0),
                fill_rate=interest / capital_days if capital_days > 0 else Decimal("0"),
            )
        )
    return out


def peak_open_principal(credits: Sequence[CreditLifetime], *, now_ms: int) -> Decimal:
    """Largest total principal the credits had lent at any one instant."""
    events: list[tuple[int, int, Decimal]] = []
    for c in credits:
        end = c.closed_ms if c.closed_ms is not None else now_ms
        if end > c.start_ms:
            # at equal timestamps a close (0) is applied before an open (1)
            events += [(c.start_ms, 1, c.amount), (end, 0, -c.amount)]
    peak = running = Decimal("0")
    for _ts, _order, delta in sorted(events):
        running += delta
        peak = max(peak, running)
    return peak


@dataclass(frozen=True)
class DeploymentCheck:
    """Peak principal the bot's credits had open vs the whole-span budget C.

    Credits are funded from the wallet whose balance is C, so with the ledger
    basis the peak cannot exceed C by more than balance drift. Above C (an
    explicit --capital below what was lent) the return on C is overstated as a
    return on a C-sized budget. Reported, never clamped: the credit model has no
    re-lent-principal double counting for a clamp to remove.
    """

    cap: Decimal
    peak_open_principal: Decimal

    @property
    def over_deployed(self) -> bool:
        return self.peak_open_principal > self.cap

    @property
    def over_deploy_factor(self) -> Decimal:
        return self.peak_open_principal / self.cap if self.cap > 0 else Decimal("0")


@dataclass(frozen=True)
class CreditCoverage:
    """Which of the symbol's credits the active arm counted, and what it left out."""

    bot_credits: int
    gross_by_cell: dict[str, Decimal]   # over the data window
    unattributed_credits: int           # no trade, or an offer that is not ours
    unattributed_gross: Decimal
    ambiguous_credits: int              # attributed, but the pairing could change the cell


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
    ledger_divergence: Sequence[str],
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
    timing alpha vs AlwaysMarketRate. ``ledger_divergence`` lists the complete
    weeks whose credit interest disagrees with the venue ledger; any forces
    UNRELIABLE regardless of the primary metric.
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

    if ledger_divergence:
        reasons.append("credit interest diverges from the venue ledger: " + "; ".join(ledger_divergence))
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


@dataclass(frozen=True)
class CellMrAlpha:
    """MR timing alpha of one bot cell against the market rate of its own period.

    The baseline lends the cell's share of C (its share of the bot's
    capital-days over the data window) at the cell's market rate, all the time.
    Unavailable when that series has no in-band point in the window; the cell
    then drops out of the total and ``reason`` says why.
    """

    cell: str
    period_agg: str
    capital_share: Decimal
    available: bool
    reason: str | None
    spread: Decimal     # full-span active − baseline, % of C
    ci_lo: Decimal
    ci_hi: Decimal


@dataclass(frozen=True)
class DataThreshold:
    """Minimum bot capital-days before the verdict may leave INSUFFICIENT_DATA."""

    capital_days: Decimal       # the bot's credits over the data window
    minimum: Decimal
    basis: str                  # how ``minimum`` was derived, for the report


# The reconciliation gate looks at this many most recent settled weeks.
GATE_WEEKS = 8


def reconciliation_status(
    r: WeeklyReconciliation, *, gate_weeks: Sequence[int], acks: Mapping[int, str],
) -> str:
    """How one reconciled week bears on the verdict."""
    if not r.complete:
        return "incomplete"
    if not r.flagged:
        return "ok"
    if r.week_start_ms not in gate_weeks:
        return "FLAG, outside gate"
    if r.week_start_ms in acks:
        return f"FLAG, acknowledged: {acks[r.week_start_ms]}"
    return "FLAG"


@dataclass(frozen=True)
class G3Report:
    """Everything the G3 report renders."""

    verdict: G3Verdict
    data_window: str
    capital_source: str                 # "ledger" or "--capital <C>"
    coverage: CreditCoverage
    deployment: DeploymentCheck
    frr: FrrBenchmark
    reconciliations: list[WeeklyReconciliation]
    reconciliation_available: bool      # False when the ledger has no payout at all
    mr_alpha_cells: list[CellMrAlpha]
    mr_alpha_coverage: Decimal          # share of bot capital-days in the MR-alpha total
    data_threshold: DataThreshold
    gate_weeks: list[int]               # week starts the reconciliation gate considers
    acknowledgements: dict[int, str]    # operator-acknowledged week start -> reason
