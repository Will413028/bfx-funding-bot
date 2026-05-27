"""Out-of-sample profitability characterization for yield/carry strategies.

Pure, I/O-free statistics over per-window backtest outcomes. The risk frame is
yield-native (utilization/idle, worst-month, downside-only Sortino, active return
vs a passive benchmark) — NOT directional drawdown, which is structurally 0 for a
lending strategy (equity is monotonic; see engine.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.backtest.sortino import compute_sortino


@dataclass(frozen=True)
class WindowOutcome:
    """One WFO test-month outcome for one arm (strategy or baseline)."""

    month_mts: int  # window.test_start_mts
    net_monthly: Decimal  # net_monthly_return_pct for the month (percent, >= 0)
    n_trades: int
    fill_rate: Decimal  # avg fill prob across trades; 0 when n_trades == 0


@dataclass(frozen=True)
class OosSummary:
    """Point-estimate distribution summary for one arm over all windows."""

    n_windows: int
    median_monthly: Decimal
    p25_monthly: Decimal
    worst_monthly: Decimal  # min (>= 0 for lending: the least-earning month)
    best_monthly: Decimal  # max
    mean_monthly: Decimal
    annualized_pct: Decimal  # geometric: (prod(1+m/100))^(12/N) - 1, percent
    idle_rate: Decimal  # fraction of windows with n_trades == 0
    mean_fill_rate: Decimal  # mean fill_rate over windows with n_trades > 0; 0 if none
    sortino: Decimal  # compute_sortino over monthly fractions; 0 if <3 windows, Decimal("Infinity") when no downside observed (the normal lending case)


def _percentile(sorted_vals: list[Decimal], q: Decimal) -> Decimal:
    """Linear-interpolated percentile (numpy 'linear' method). q in [0, 1].

    Caller must pass an ascending-sorted, non-empty list.
    """
    if not sorted_vals:
        raise ValueError("_percentile: empty list")
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * Decimal(n - 1)
    lo = int(pos)  # floor toward zero; pos >= 0
    frac = pos - Decimal(lo)
    if lo + 1 >= n:
        return sorted_vals[lo]
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


def summarize_oos(outcomes: list[WindowOutcome]) -> OosSummary:
    """Distribution + yield-native risk summary over per-window outcomes."""
    if not outcomes:
        raise ValueError("summarize_oos: no outcomes")
    n = len(outcomes)
    monthly = sorted(o.net_monthly for o in outcomes)

    growth = Decimal("1")
    for m in monthly:
        growth *= Decimal("1") + m / Decimal("100")
    annualized = (growth ** (Decimal("12") / Decimal(n)) - Decimal("1")) * Decimal("100")

    idle = sum(1 for o in outcomes if o.n_trades == 0)
    active_fills = [o.fill_rate for o in outcomes if o.n_trades > 0]
    mean_fill = (
        sum(active_fills, Decimal("0")) / Decimal(len(active_fills))
        if active_fills
        else Decimal("0")
    )

    sortino = compute_sortino([m / Decimal("100") for m in monthly])

    return OosSummary(
        n_windows=n,
        median_monthly=_percentile(monthly, Decimal("0.5")),
        p25_monthly=_percentile(monthly, Decimal("0.25")),
        worst_monthly=monthly[0],
        best_monthly=monthly[-1],
        mean_monthly=sum(monthly, Decimal("0")) / Decimal(n),
        annualized_pct=annualized,
        idle_rate=Decimal(idle) / Decimal(n),
        mean_fill_rate=mean_fill,
        sortino=sortino,
    )


@dataclass(frozen=True)
class ActiveReturnSummary:
    """Strategy minus passive benchmark, paired per window."""

    n_windows: int
    median_active: Decimal  # median of (strat - base) per window, percent
    mean_active: Decimal
    information_ratio: Decimal  # mean_active / population std; +inf if std==0 & mean>0; 0 if std==0 & mean<=0
    pct_months_outperform: Decimal  # fraction of windows with strat strictly > base


def active_return_summary(
    strat: list[WindowOutcome], base: list[WindowOutcome]
) -> ActiveReturnSummary:
    """Paired active-return stats. Both lists must align 1:1 by month_mts (same order)."""
    if len(strat) != len(base):
        raise ValueError(
            f"active_return_summary: length mismatch {len(strat)} != {len(base)}"
        )
    if not strat:
        raise ValueError("active_return_summary: no outcomes")
    actives: list[Decimal] = []
    outperform = 0
    for s, b in zip(strat, base, strict=True):
        if s.month_mts != b.month_mts:
            raise ValueError(
                f"active_return_summary: misaligned month {s.month_mts} != {b.month_mts}"
            )
        diff = s.net_monthly - b.net_monthly
        actives.append(diff)
        if diff > 0:
            outperform += 1

    n = len(actives)
    mean = sum(actives, Decimal("0")) / Decimal(n)
    var = sum(((a - mean) ** 2 for a in actives), Decimal("0")) / Decimal(n)  # population
    std = var.sqrt()
    if std == 0:  # noqa: SIM108  # two zero-std sub-cases clearer as explicit branches
        ir = Decimal("Infinity") if mean > 0 else Decimal("0")
    else:
        ir = mean / std

    return ActiveReturnSummary(
        n_windows=n,
        median_active=_percentile(sorted(actives), Decimal("0.5")),
        mean_active=mean,
        information_ratio=ir,
        pct_months_outperform=Decimal(outperform) / Decimal(n),
    )
