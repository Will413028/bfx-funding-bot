"""Period-threshold research — lend 2d by default, a longer tenor only above a rate threshold.

Research-only (2026-09-27 question: should the bot switch from "always 2-day
offers" to a threshold rule?). Nothing here is imported by the live bot.

Why a separate simulator instead of `engine.run_backtest`: the engine holds every
credit for its full period (engine.py cooldown = period * 24 h). Bitfinex
borrowers can return funding at any time and pay interest only for the time
used, so a long lock is an option held by the *borrower*. This module models
that explicitly with `RepaymentScenario`:

* `held_fraction` — every credit (2d and long alike) is returned after that
  share of its term (1.0 = the engine's assumption);
* `refinance_on_drop` — the adverse case: the borrower returns the credit the
  first hour the 2d market prices more than `refinance_margin` below the locked
  rate (they can refinance cheaper), which removes exactly the upside a long
  lock was meant to capture;
* `relend_delay_hours` — idle hours between a credit ending and the capital
  being lent again.

After any return the capital goes back through the same rule at the then-current
rates. Interest is simple (not compounded) on one unit of capital; APR =
interest over the window / window years, net of the platform fee.

Inputs are hourly grids: `r2[i]` is the 2d rate reference for hour i, `rlong[i]`
the long-tenor rate reference; `None` means no price reference that hour (no
trade / no visible exact-period ask), in which case the tenor cannot be lent.
Rates are daily fractions (0.0002 = 0.02 %/day = 7.3 % APR).
"""
from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from statistics import median

HOURS_PER_YEAR = 24 * 365
DEFAULT_FEE = 0.15  # Bitfinex takes 15 % of funding interest (backtest.config.fee_rate)


@dataclass(frozen=True, slots=True)
class RepaymentScenario:
    name: str
    held_fraction: float = 1.0
    relend_delay_hours: int = 1
    refinance_on_drop: bool = False
    refinance_margin: float = 0.2

    def __post_init__(self) -> None:
        if not 0 <= self.refinance_margin < 1:
            raise ValueError("refinance_margin must be in [0, 1)")
        if not 0 < self.held_fraction <= 1:
            raise ValueError(f"held_fraction must be in (0, 1], got {self.held_fraction}")
        if self.relend_delay_hours < 0:
            raise ValueError("relend_delay_hours must be >= 0")


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """Lend `long_period_days` when the gate passes, else 2d.

    Gate (all must hold): a long price reference exists, `rlong >= r2` (never lock
    longer for less than the 2d market), and either `rlong >= min_long_rate` or
    `r2 >= trailing percentile` of the 2d rate. With neither threshold set the
    rule never goes long (the always-2d baseline).
    """

    name: str
    long_period_days: int = 2
    min_long_rate: float | None = None
    r2_percentile: float | None = None
    lookback_hours: int = 720

    def __post_init__(self) -> None:
        if self.min_long_rate is not None and self.r2_percentile is not None:
            raise ValueError("set min_long_rate or r2_percentile, not both")
        if self.r2_percentile is not None and not 0 < self.r2_percentile < 1:
            raise ValueError("r2_percentile must be in (0, 1)")

    @property
    def goes_long(self) -> bool:
        return self.long_period_days > 2 and (
            self.min_long_rate is not None or self.r2_percentile is not None
        )


ALWAYS_2D = ThresholdRule(name="always_2d")


@dataclass(frozen=True, slots=True)
class SimResult:
    rule: str
    scenario: str
    hours: int
    gross_apr: float
    net_apr: float
    long_capital_share: float  # share of window hours with capital in a long credit
    idle_share: float
    n_long: int
    n_short: int


def trailing_percentile(
    values: Sequence[float | None], q: float, lookback: int, min_obs: int | None = None
) -> list[float | None]:
    """q-quantile (nearest rank) of the previous `lookback` non-None values, no lookahead.

    Slot i uses values[i-lookback : i]; returns None until `min_obs` (default
    lookback // 2) observations are available.
    """
    need = lookback // 2 if min_obs is None else min_obs
    window: list[float] = []
    out: list[float | None] = []
    for i, _ in enumerate(values):
        if i - lookback - 1 >= 0:
            old = values[i - lookback - 1]
            if old is not None:
                del window[bisect.bisect_left(window, old)]
        if i >= 1:
            prev = values[i - 1]
            if prev is not None:
                bisect.insort(window, prev)
        if len(window) < max(need, 1):
            out.append(None)
            continue
        rank = max(1, math.ceil(q * len(window)))
        out.append(window[rank - 1])
    return out


def simulate(
    r2: Sequence[float | None],
    rlong: Sequence[float | None],
    rule: ThresholdRule,
    scenario: RepaymentScenario,
    *,
    fee: float = DEFAULT_FEE,
    r2_threshold: Sequence[float | None] | None = None,
) -> SimResult:
    """`r2_threshold` lets a caller pass a precomputed trailing percentile (e.g.
    computed over the full history before slicing a month), so a slice does not
    lose its first lookback of signal."""
    n = len(r2)
    if len(rlong) != n:
        raise ValueError("r2 and rlong must be aligned hourly grids of equal length")
    pct: Sequence[float | None] | None = None
    if rule.goes_long and rule.r2_percentile is not None:
        pct = (
            r2_threshold
            if r2_threshold is not None
            else trailing_percentile(r2, rule.r2_percentile, rule.lookback_hours)
        )
    interest = 0.0
    long_hours = 0
    lent_hours = 0
    n_long = n_short = 0
    i = 0
    while i < n:
        short = r2[i]
        long_ = rlong[i]
        go_long = False
        if rule.goes_long and long_ is not None and (short is None or long_ >= short):
            if rule.min_long_rate is not None:
                go_long = long_ >= rule.min_long_rate
            elif pct is not None and short is not None and pct[i] is not None:
                go_long = short >= pct[i]  # type: ignore[operator]
        if go_long:
            rate, term = long_, rule.long_period_days * 24
        elif short is not None:
            rate, term = short, 48
        else:
            i += 1
            continue
        assert rate is not None
        held = max(1, round(term * scenario.held_fraction))
        if scenario.refinance_on_drop:
            trigger = rate * (1 - scenario.refinance_margin)
            for j in range(i + 1, min(i + held, n)):
                rj = r2[j]
                if rj is not None and rj < trigger:
                    held = j - i
                    break
        held = min(held, n - i)
        interest += rate * held / 24
        lent_hours += held
        if go_long:
            long_hours += held
            n_long += 1
        else:
            n_short += 1
        i += held + scenario.relend_delay_hours
    years = n / HOURS_PER_YEAR if n else 0.0
    gross = interest / years if years else 0.0
    return SimResult(
        rule=rule.name,
        scenario=scenario.name,
        hours=n,
        gross_apr=gross,
        net_apr=gross * (1 - fee),
        long_capital_share=long_hours / n if n else 0.0,
        idle_share=1 - lent_hours / n if n else 0.0,
        n_long=n_long,
        n_short=n_short,
    )


def limit_fill_grid(
    close: Sequence[float | None],
    high: Sequence[float | None],
    cap: float | None = None,
) -> list[float | None]:
    """Executable rate per hour for a limit offer priced off the *previous* hour.

    An offer posted at the start of hour i at close[i-1] fills during hour i only
    if some trade of that tenor printed at or above it (high[i] >= close[i-1]).
    This removes the same-hour lookahead of lending at the hour's own close and
    stops a single spike print from being "captured" unless the market traded
    there again. `cap` clips rates (a no-spike robustness view).
    """
    out: list[float | None] = [None] * len(close)
    for i in range(1, len(close)):
        price, hi = close[i - 1], high[i]
        if price is None or hi is None or hi < price:
            continue
        out[i] = price if cap is None else min(price, cap)
    return out


@lru_cache(maxsize=32)
def _month_slices(start_ms: int, n: int) -> tuple[tuple[int, int], ...]:
    """[lo, hi) index ranges of each UTC calendar month on an hourly grid."""
    out: list[tuple[int, int]] = []
    lo = 0
    prev: str | None = None
    for i in range(n):
        key = datetime.fromtimestamp((start_ms + i * 3_600_000) / 1000, tz=UTC).strftime("%Y-%m")
        if prev is not None and key != prev:
            out.append((lo, i))
            lo = i
        prev = key
    if n:
        out.append((lo, n))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class MonthlyDelta:
    months: int
    median_delta: float
    p10_delta: float
    win_share: float


def monthly_deltas(
    start_ms: int,
    r2: Sequence[float | None],
    rlong: Sequence[float | None],
    rule: ThresholdRule,
    scenario: RepaymentScenario,
    *,
    fee: float = DEFAULT_FEE,
) -> MonthlyDelta:
    """Simulate each calendar month independently; net-APR delta of `rule` vs always-2d.

    A fat-tail month cannot dominate a median, so this is the robust read next to
    the full-window APR. Credits are truncated at month end for both arms.
    """
    pct = (
        trailing_percentile(r2, rule.r2_percentile, rule.lookback_hours)
        if rule.goes_long and rule.r2_percentile is not None
        else None
    )
    deltas: list[float] = []
    for lo, hi in _month_slices(start_ms, len(r2)):
        a = simulate(
            r2[lo:hi],
            rlong[lo:hi],
            rule,
            scenario,
            fee=fee,
            r2_threshold=None if pct is None else pct[lo:hi],
        )
        b = simulate(r2[lo:hi], rlong[lo:hi], ALWAYS_2D, scenario, fee=fee)
        deltas.append(a.net_apr - b.net_apr)
    if not deltas:
        return MonthlyDelta(0, 0.0, 0.0, 0.0)
    srt = sorted(deltas)
    return MonthlyDelta(
        months=len(deltas),
        median_delta=median(srt),
        p10_delta=srt[max(0, math.ceil(0.1 * len(srt)) - 1)],
        win_share=sum(1 for d in deltas if d > 0) / len(deltas),
    )


def hourly_grid(
    points: Sequence[tuple[int, float]], start_ms: int, end_ms: int
) -> list[float | None]:
    """Place (mts, rate) points on an hourly grid [start_ms, end_ms); later points win."""
    n = (end_ms - start_ms) // 3_600_000
    grid: list[float | None] = [None] * max(n, 0)
    for mts, rate in points:
        idx = (mts - start_ms) // 3_600_000
        if 0 <= idx < n:
            grid[idx] = rate
    return grid


@dataclass(frozen=True, slots=True)
class PremiumRow:
    bucket: str
    n_hours: int
    long_available_share: float
    median_premium: float | None  # median of rlong / r2 - 1 over hours with both
    median_r2: float | None


def premium_by_bucket(
    start_ms: int,
    r2: Sequence[float | None],
    rlong: Sequence[float | None],
    bucket: str = "year",
) -> list[PremiumRow]:
    """Long-tenor premium over 2d per calendar year (or month, bucket='month')."""
    fmt = "%Y" if bucket == "year" else "%Y-%m"
    groups: dict[str, list[int]] = {}
    for i in range(len(r2)):
        key = datetime.fromtimestamp((start_ms + i * 3_600_000) / 1000, tz=UTC).strftime(fmt)
        groups.setdefault(key, []).append(i)
    rows: list[PremiumRow] = []
    for key in sorted(groups):
        idx = groups[key]
        both = [
            (rlong[i], r2[i])
            for i in idx
            if rlong[i] is not None and r2[i] is not None and r2[i] > 0  # type: ignore[operator]
        ]
        shorts = [r2[i] for i in idx if r2[i] is not None]
        rows.append(
            PremiumRow(
                bucket=key,
                n_hours=len(idx),
                long_available_share=sum(1 for i in idx if rlong[i] is not None) / len(idx),
                median_premium=median(lo / s - 1 for lo, s in both) if both else None,  # type: ignore[operator]
                median_r2=median(shorts) if shorts else None,  # type: ignore[type-var]
            )
        )
    return rows
