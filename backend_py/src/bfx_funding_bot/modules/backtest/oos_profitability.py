"""Out-of-sample profitability characterization for yield/carry strategies.

Pure, I/O-free statistics over per-window backtest outcomes. The risk frame is
yield-native (utilization/idle, worst-month, downside-only Sortino, active return
vs a passive benchmark) — NOT directional drawdown, which is structurally 0 for a
lending strategy (equity is monotonic; see engine.py).
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from statistics import NormalDist

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


def bootstrap_ci(
    values: list[Decimal],
    stat_fn: Callable[[list[Decimal]], Decimal],
    *,
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[Decimal, Decimal]:
    """Percentile bootstrap CI for stat_fn over values.

    Resamples `values` with replacement n_resamples times, computes stat_fn on each
    resample, and returns the (alpha/2, 1-alpha/2) percentiles of that distribution.
    Deterministic given `seed`.
    """
    if not values:
        raise ValueError("bootstrap_ci: empty values")
    rng = random.Random(seed)
    n = len(values)
    stats: list[Decimal] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats.append(stat_fn(sample))
    stats.sort()
    lo = _percentile(stats, Decimal(str(alpha / 2)))
    hi = _percentile(stats, Decimal(str(1 - alpha / 2)))
    return lo, hi


_GAMMA = 0.5772156649015329  # Euler-Mascheroni
_NORM = NormalDist()


def sharpe_skew_kurt(returns: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Return (Sharpe, skewness, raw kurtosis) of a return series.

    Sharpe = mean/std (population, ddof=0). Kurtosis is the raw 4th standardized
    moment (3.0 for a normal). Requires >= 3 observations.
    """
    n = len(returns)
    if n < 3:
        raise ValueError(f"sharpe_skew_kurt: need >= 3 returns, got {n}")
    fl = [float(r) for r in returns]
    mean = sum(fl) / n
    var = sum((x - mean) ** 2 for x in fl) / n
    if var == 0:
        return Decimal("0"), Decimal("0"), Decimal("0")
    std = math.sqrt(var)
    sharpe = mean / std
    skew = (sum((x - mean) ** 3 for x in fl) / n) / std**3
    kurt = (sum((x - mean) ** 4 for x in fl) / n) / std**4
    return Decimal(str(sharpe)), Decimal(str(skew)), Decimal(str(kurt))


def deflated_sharpe(
    observed_sharpe: Decimal,
    *,
    n_trials: int,
    n_obs: int,
    skew: Decimal,
    kurtosis: Decimal,
) -> Decimal:
    """Deflated Sharpe Ratio (Bailey & López de Prado) — probability in [0,1].

    Probabilistic Sharpe vs SR0 = expected max Sharpe under the null across n_trials
    independent configs. > 0.95 => edge survives selection-bias deflation.
    Returns Decimal("0") if the variance term is degenerate (denom_inner <= 0).
    """
    if n_trials < 1:
        raise ValueError(f"deflated_sharpe: n_trials must be >= 1, got {n_trials}")
    if n_obs < 2:
        raise ValueError(f"deflated_sharpe: n_obs must be >= 2, got {n_obs}")
    sr = float(observed_sharpe)

    if n_trials == 1:
        sr0 = 0.0
    else:
        n = float(n_trials)
        sr0 = math.sqrt(1.0 / n_obs) * (
            (1 - _GAMMA) * _NORM.inv_cdf(1 - 1.0 / n)
            + _GAMMA * _NORM.inv_cdf(1 - 1.0 / (n * math.e))
        )

    denom_inner = 1 - float(skew) * sr + (float(kurtosis) - 1) / 4 * sr**2
    if denom_inner <= 0:
        return Decimal("0")

    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom_inner)
    return Decimal(str(_NORM.cdf(z)))
