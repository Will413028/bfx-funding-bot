# Canary OOS Profitability Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Characterize the out-of-sample profitability and yield-native risk of the exact deployed canary config (MeanReversion × fUST × {a30, p2}) vs the AlwaysFRR passive benchmark, and emit a research doc with bootstrap CIs and a selection-bias deflation check.

**Architecture:** A new pure, I/O-free metrics module (`modules/backtest/oos_profitability.py`) holds all statistics (distribution summary, active-return/information-ratio, bootstrap CI, deflated Sharpe) — fully unit-testable with no DB. A thin script (`scripts/run_oos_profitability.py`) loads candles from Neon, reuses `compute_wfo_windows` + `run_backtest` to produce per-window outcomes for both arms, feeds them to the pure module, and writes `docs/research/2026-05-28-canary-oos-profitability.md` + `.json`. No engine changes; idle is measured from trade counts, not simulated.

**Tech Stack:** Python 3.13, Decimal arithmetic, `statistics.NormalDist` (stdlib, for the deflated-Sharpe normal CDF/inverse — no scipy), SQLAlchemy async (existing `get_candles_in_range`), pytest. All commands run from `backend_py/` via `uv run`.

**Spec:** `docs/superpowers/specs/2026-05-28-canary-oos-profitability-validation-design.md`

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py` | Pure metrics: `WindowOutcome`, `OosSummary`, `ActiveReturnSummary`, `summarize_oos`, `active_return_summary`, `bootstrap_ci`, `sharpe_skew_kurt`, `deflated_sharpe`, `_percentile` | Create |
| `backend_py/tests/modules/backtest/test_oos_profitability.py` | Unit tests for all pure functions | Create |
| `backend_py/scripts/run_oos_profitability.py` | CLI orchestration: load candles → windows → run_backtest per arm → summarize → write md+json | Create |
| `backend_py/tests/scripts/test_run_oos_profitability.py` | Unit test for the script's pure helpers (param mapping, doc rendering) | Create |
| `docs/research/2026-05-28-canary-oos-profitability.md` + `.json` | The deliverable report | Create (Task 7) |

**Reused unchanged:** `modules/backtest/wfo.py` (`compute_wfo_windows`), `engine.py` (`run_backtest`), `strategies/{mean_reversion,always_frr}.py`, `sortino.py` (`compute_sortino`), `candles/repository.py` (`get_candles_in_range`).

**Key reused signatures (verified):**
- `compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1, min_candles_per_segment=200) -> list[WfoWindow]`; `WfoWindow` has `train_start_mts, train_end_mts, test_start_mts, test_end_mts` (all inclusive).
- `run_backtest(candles, strategy, config=None, record_start_mts=None, record_end_mts=None, fill_model=None) -> BacktestResult`; `BacktestResult` has `net_monthly_return_pct, n_trades, fill_rate, max_drawdown_pct, sortino, n_candles` (Decimals/ints).
- `MeanReversionStrategy(ema_span: int, threshold_sigma: Decimal, ratio_sigma: Decimal)`.
- `AlwaysFRRStrategy(period_days: int = 2)`.
- `compute_sortino(returns: list[Decimal]) -> Decimal` — returns `Decimal("0")` if `len<3`, `Decimal("Infinity")` if no downside; expects returns as **fractions** (e.g. 0.02 for 2%).
- `get_candles_in_range(session, *, symbol, timeframe, period_agg, start_mts, end_mts) -> list[FundingCandle]` (async).
- `BacktestConfig()` default has `fill_model="empirical"`; with `fill_model=None` passed to `run_backtest`, `_apply_friction` falls back to the deterministic linear model. **This plan uses the linear fill model** (`BacktestConfig(fill_model="linear")`) for reproducibility — documented in the report as a methodology choice.

---

## Task 1: Distribution summary (`WindowOutcome`, `OosSummary`, `summarize_oos`)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py`
- Test: `backend_py/tests/modules/backtest/test_oos_profitability.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend_py/tests/modules/backtest/test_oos_profitability.py
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.oos_profitability import (
    OosSummary,
    WindowOutcome,
    _percentile,
    summarize_oos,
)


def _w(month_mts: int, net_monthly: str, n_trades: int = 5, fill_rate: str = "0.8") -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts,
        net_monthly=Decimal(net_monthly),
        n_trades=n_trades,
        fill_rate=Decimal(fill_rate),
    )


def test_percentile_linear_interpolation():
    vals = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
    assert _percentile(vals, Decimal("0.5")) == Decimal("2.5")
    assert _percentile(vals, Decimal("0")) == Decimal("1")
    assert _percentile(vals, Decimal("1")) == Decimal("4")
    # q=0.25 over 4 points: pos = 0.25*3 = 0.75 -> 1 + 0.75*(2-1) = 1.75
    assert _percentile(vals, Decimal("0.25")) == Decimal("1.75")


def test_percentile_single_value():
    assert _percentile([Decimal("7")], Decimal("0.9")) == Decimal("7")


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        _percentile([], Decimal("0.5"))


def test_summarize_basic_distribution():
    outcomes = [
        _w(0, "1.0"),
        _w(1, "2.0"),
        _w(2, "3.0"),
        _w(3, "4.0"),
    ]
    s = summarize_oos(outcomes)
    assert s.n_windows == 4
    assert s.median_monthly == Decimal("2.5")
    assert s.p25_monthly == Decimal("1.75")
    assert s.worst_monthly == Decimal("1.0")
    assert s.best_monthly == Decimal("4.0")
    assert s.mean_monthly == Decimal("2.5")


def test_summarize_annualized_is_geometric():
    # 12 windows each +1% -> annualized = (1.01^12)^(12/12) - 1 = 1.01^12 - 1
    outcomes = [_w(i, "1.0") for i in range(12)]
    s = summarize_oos(outcomes)
    expected = (Decimal("1.01") ** 12 - Decimal("1")) * Decimal("100")
    assert abs(s.annualized_pct - expected) < Decimal("0.0001")


def test_summarize_idle_rate_and_fill():
    outcomes = [
        _w(0, "2.0", n_trades=10, fill_rate="0.9"),
        _w(1, "0.0", n_trades=0, fill_rate="0"),  # idle month
        _w(2, "1.0", n_trades=4, fill_rate="0.5"),
    ]
    s = summarize_oos(outcomes)
    assert s.idle_rate == Decimal("1") / Decimal("3")
    # mean_fill_rate only over windows with trades: (0.9 + 0.5)/2 = 0.7
    assert s.mean_fill_rate == Decimal("0.7")


def test_summarize_all_idle_fill_zero():
    outcomes = [_w(0, "0.0", n_trades=0, fill_rate="0"), _w(1, "0.0", n_trades=0, fill_rate="0")]
    s = summarize_oos(outcomes)
    assert s.idle_rate == Decimal("1")
    assert s.mean_fill_rate == Decimal("0")


def test_summarize_sortino_small_sample_is_zero():
    # <3 windows -> compute_sortino returns 0 (untrustworthy)
    s = summarize_oos([_w(0, "1.0"), _w(1, "2.0")])
    assert s.sortino == Decimal("0")


def test_summarize_empty_raises():
    with pytest.raises(ValueError):
        summarize_oos([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -v`
Expected: FAIL — `ModuleNotFoundError: ... oos_profitability`

- [ ] **Step 3: Write the module (types + `_percentile` + `summarize_oos`)**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py
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
    sortino: Decimal  # compute_sortino over monthly fractions (0 if <3 windows)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py backend_py/tests/modules/backtest/test_oos_profitability.py
git commit -m "✨ Feat: OOS profitability distribution summary (yield-native metrics)"
```

---

## Task 2: Active return vs passive benchmark (`ActiveReturnSummary`, `active_return_summary`)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py`
- Test: `backend_py/tests/modules/backtest/test_oos_profitability.py` (append)

- [ ] **Step 1: Write the failing tests (append)**

```python
# append to test_oos_profitability.py
from bfx_funding_bot.modules.backtest.oos_profitability import (  # noqa: E402
    ActiveReturnSummary,
    active_return_summary,
)


def test_active_return_paired_by_month():
    strat = [_w(0, "3.0"), _w(1, "2.0"), _w(2, "4.0")]
    base = [_w(0, "1.0"), _w(1, "2.0"), _w(2, "1.0")]
    a = active_return_summary(strat, base)
    assert a.n_windows == 3
    # actives: 2.0, 0.0, 3.0 -> median 2.0, mean 5/3
    assert a.median_active == Decimal("2.0")
    assert a.mean_active == Decimal("5") / Decimal("3")
    # outperform = strat strictly > base in 2 of 3 windows
    assert a.pct_months_outperform == Decimal("2") / Decimal("3")


def test_active_return_information_ratio_zero_std():
    # constant active return -> std 0, mean > 0 -> IR = +inf
    strat = [_w(0, "3.0"), _w(1, "3.0"), _w(2, "3.0")]
    base = [_w(0, "1.0"), _w(1, "1.0"), _w(2, "1.0")]
    a = active_return_summary(strat, base)
    assert a.information_ratio == Decimal("Infinity")


def test_active_return_information_ratio_all_zero():
    strat = [_w(0, "1.0"), _w(1, "1.0"), _w(2, "1.0")]
    base = [_w(0, "1.0"), _w(1, "1.0"), _w(2, "1.0")]
    a = active_return_summary(strat, base)
    assert a.information_ratio == Decimal("0")


def test_active_return_misaligned_months_raises():
    strat = [_w(0, "3.0"), _w(1, "2.0")]
    base = [_w(0, "1.0"), _w(99, "1.0")]
    with pytest.raises(ValueError):
        active_return_summary(strat, base)


def test_active_return_length_mismatch_raises():
    with pytest.raises(ValueError):
        active_return_summary([_w(0, "1.0")], [_w(0, "1.0"), _w(1, "1.0")])
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -k active -v`
Expected: FAIL — `ImportError: cannot import name 'active_return_summary'`

- [ ] **Step 3: Implement (append to module)**

```python
# append to oos_profitability.py (after summarize_oos)
@dataclass(frozen=True)
class ActiveReturnSummary:
    """Strategy minus passive benchmark, paired per window."""

    n_windows: int
    median_active: Decimal  # median of (strat - base) per window, percent
    mean_active: Decimal
    information_ratio: Decimal  # mean_active / population std; +inf if std 0 & mean>0; 0 if degenerate
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
    if std == 0:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -v`
Expected: PASS (all Task 1 + Task 2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py backend_py/tests/modules/backtest/test_oos_profitability.py
git commit -m "✨ Feat: active-return / information-ratio vs passive benchmark"
```

---

## Task 3: Bootstrap confidence intervals (`bootstrap_ci`)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py`
- Test: `backend_py/tests/modules/backtest/test_oos_profitability.py` (append)

- [ ] **Step 1: Write the failing tests (append)**

```python
# append to test_oos_profitability.py
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci  # noqa: E402


def _mean(vals: list[Decimal]) -> Decimal:
    return sum(vals, Decimal("0")) / Decimal(len(vals))


def test_bootstrap_ci_deterministic_with_seed():
    vals = [Decimal(str(x)) for x in range(1, 21)]  # 1..20
    lo1, hi1 = bootstrap_ci(vals, _mean, n_resamples=500, seed=42)
    lo2, hi2 = bootstrap_ci(vals, _mean, n_resamples=500, seed=42)
    assert (lo1, hi1) == (lo2, hi2)  # same seed -> identical


def test_bootstrap_ci_brackets_point_estimate():
    vals = [Decimal(str(x)) for x in range(1, 21)]
    point = _mean(vals)  # 10.5
    lo, hi = bootstrap_ci(vals, _mean, n_resamples=2000, seed=7)
    assert lo < point < hi


def test_bootstrap_ci_degenerate_all_equal():
    vals = [Decimal("3")] * 10
    lo, hi = bootstrap_ci(vals, _mean, n_resamples=200, seed=1)
    assert lo == Decimal("3") and hi == Decimal("3")


def test_bootstrap_ci_empty_raises():
    with pytest.raises(ValueError):
        bootstrap_ci([], _mean, n_resamples=10, seed=1)
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -k bootstrap -v`
Expected: FAIL — `ImportError: cannot import name 'bootstrap_ci'`

- [ ] **Step 3: Implement (append to module)**

```python
# add near top imports of oos_profitability.py:
import random
from collections.abc import Callable

# append after active_return_summary:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py backend_py/tests/modules/backtest/test_oos_profitability.py
git commit -m "✨ Feat: percentile bootstrap CI (deterministic, seeded)"
```

---

## Task 4: Selection-bias deflation (`sharpe_skew_kurt`, `deflated_sharpe`)

**Background:** Deflated Sharpe Ratio (Bailey & López de Prado). We compute the Probabilistic Sharpe Ratio against a non-zero benchmark `SR0` = the expected maximum Sharpe under the null across `n_trials` independent configs. Self-contained — uses `statistics.NormalDist` for Φ and Φ⁻¹, with `Var(SR_trials) ≈ 1/n_obs` (the variance of the Sharpe estimator under the null). Returns a probability in [0,1]; > 0.95 means the edge survives deflation.

Formulas:
- `gamma = 0.5772156649015329` (Euler–Mascheroni)
- For `n_trials >= 2`: `SR0 = sqrt(1/n_obs) * ((1-gamma)*Φ⁻¹(1 - 1/N) + gamma*Φ⁻¹(1 - 1/(N*e)))`; for `n_trials == 1`: `SR0 = 0`.
- `denom_inner = 1 - skew*SR + ((kurt - 1)/4)*SR**2` (kurt = raw 4th standardized moment; 3.0 for normal). If `denom_inner <= 0` → return `Decimal("0")` (undefined).
- `DSR = Φ( (SR - SR0) * sqrt(n_obs - 1) / sqrt(denom_inner) )`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py`
- Test: `backend_py/tests/modules/backtest/test_oos_profitability.py` (append)

- [ ] **Step 1: Write the failing tests (append)**

```python
# append to test_oos_profitability.py
import math  # noqa: E402
from statistics import NormalDist  # noqa: E402

from bfx_funding_bot.modules.backtest.oos_profitability import (  # noqa: E402
    deflated_sharpe,
    sharpe_skew_kurt,
)


def test_sharpe_skew_kurt_symmetric_series():
    # symmetric returns -> skew ~ 0
    rets = [Decimal("-2"), Decimal("-1"), Decimal("0"), Decimal("1"), Decimal("2")]
    sr, skew, kurt = sharpe_skew_kurt(rets)
    assert sr == Decimal("0")  # mean 0
    assert abs(skew) < Decimal("0.0001")
    assert kurt > Decimal("1")  # raw kurtosis positive


def test_sharpe_skew_kurt_too_few_raises():
    with pytest.raises(ValueError):
        sharpe_skew_kurt([Decimal("1"), Decimal("2")])


def test_deflated_sharpe_single_trial_equals_psr_vs_zero():
    # n_trials=1 -> SR0=0 -> DSR == Φ(SR*sqrt(n-1)/sqrt(denom)) with skew 0, kurt 3
    sr = Decimal("0.5")
    n_obs = 50
    dsr = deflated_sharpe(sr, n_trials=1, n_obs=n_obs, skew=Decimal("0"), kurtosis=Decimal("3"))
    denom = math.sqrt(1 + 0.5 * 0.5**2)  # 1 - 0 + (3-1)/4 * SR^2
    expected = NormalDist().cdf(0.5 * math.sqrt(n_obs - 1) / denom)
    assert abs(float(dsr) - expected) < 1e-6


def test_deflated_sharpe_decreases_with_more_trials():
    sr = Decimal("0.5")
    d1 = deflated_sharpe(sr, n_trials=1, n_obs=50, skew=Decimal("0"), kurtosis=Decimal("3"))
    d9 = deflated_sharpe(sr, n_trials=9, n_obs=50, skew=Decimal("0"), kurtosis=Decimal("3"))
    assert d9 < d1  # more trials -> harder to clear -> lower probability


def test_deflated_sharpe_degenerate_denominator_returns_zero():
    # craft skew/kurt making denom_inner <= 0
    dsr = deflated_sharpe(
        Decimal("2"), n_trials=2, n_obs=50, skew=Decimal("5"), kurtosis=Decimal("3")
    )
    assert dsr == Decimal("0")


def test_deflated_sharpe_n_obs_too_small_raises():
    with pytest.raises(ValueError):
        deflated_sharpe(Decimal("0.5"), n_trials=2, n_obs=1, skew=Decimal("0"), kurtosis=Decimal("3"))


def test_deflated_sharpe_n_trials_zero_raises():
    with pytest.raises(ValueError):
        deflated_sharpe(Decimal("0.5"), n_trials=0, n_obs=50, skew=Decimal("0"), kurtosis=Decimal("3"))
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -k "sharpe or deflated" -v`
Expected: FAIL — `ImportError: cannot import name 'deflated_sharpe'`

- [ ] **Step 3: Implement (append to module)**

```python
# add to imports of oos_profitability.py:
import math
from statistics import NormalDist

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
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -v`
Expected: PASS (all tasks 1–4)

- [ ] **Step 5: Run full quality gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS / no errors. Fix any mypy/ruff issues in the new module inline.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/oos_profitability.py backend_py/tests/modules/backtest/test_oos_profitability.py
git commit -m "✨ Feat: deflated Sharpe (selection-bias) + sharpe/skew/kurt helper"
```

---

## Task 5: Orchestration script (`run_oos_profitability.py`)

**Files:**
- Create: `backend_py/scripts/run_oos_profitability.py`
- Test: `backend_py/tests/scripts/test_run_oos_profitability.py`

The script: (1) defines the two canary cells with production params; (2) for each cell, loads candles from Neon, computes WFO windows, runs `run_backtest` per window for strategy + baseline → `WindowOutcome`s; (3) summarizes; (4) renders markdown + JSON. Keep DB-touching code in `async def main`; keep param mapping and doc rendering as **pure helpers** so they can be unit-tested without a DB.

- [ ] **Step 1: Write failing tests for the pure helpers**

```python
# backend_py/tests/scripts/test_run_oos_profitability.py
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "run_oos_profitability",
    Path(__file__).resolve().parents[2] / "scripts" / "run_oos_profitability.py",
)
mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(mod)


def test_alpha_to_ema_span():
    # 0.01183 -> round(2/0.01183 - 1) = round(168.06) = 168
    assert mod.alpha_to_ema_span(Decimal("0.01183")) == 168
    # 0.5 -> round(2/0.5 - 1) = round(3) = 3
    assert mod.alpha_to_ema_span(Decimal("0.5")) == 3


def test_alpha_to_ema_span_invalid_raises():
    with pytest.raises(ValueError):
        mod.alpha_to_ema_span(Decimal("0"))


def test_canary_cells_match_config():
    # the two shipped cells, fUST only, mean_reversion
    cells = mod.CANARY_CELLS
    assert len(cells) == 2
    assert {c.period_agg for c in cells} == {"a30", "p2"}
    assert all(c.symbol == "fUST" and c.strategy == "mean_reversion" for c in cells)


def test_render_markdown_contains_key_sections():
    # build a minimal CellReport and ensure rendered md has the required headers
    report = mod.CellReport(
        cell_label="fUST_a30",
        n_windows=3,
        strat_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.5"), p25_monthly=Decimal("0.3"),
            worst_monthly=Decimal("0.1"), best_monthly=Decimal("0.9"),
            mean_monthly=Decimal("0.5"), annualized_pct=Decimal("6.2"),
            idle_rate=Decimal("0.1"), mean_fill_rate=Decimal("0.8"), sortino=Decimal("1.2"),
        ),
        base_summary=mod.OosSummary(
            n_windows=3, median_monthly=Decimal("0.4"), p25_monthly=Decimal("0.2"),
            worst_monthly=Decimal("0.05"), best_monthly=Decimal("0.8"),
            mean_monthly=Decimal("0.4"), annualized_pct=Decimal("4.9"),
            idle_rate=Decimal("0"), mean_fill_rate=Decimal("0.9"), sortino=Decimal("1.0"),
        ),
        active=mod.ActiveReturnSummary(
            n_windows=3, median_active=Decimal("0.1"), mean_active=Decimal("0.1"),
            information_ratio=Decimal("0.5"), pct_months_outperform=Decimal("0.66"),
        ),
        median_ci=(Decimal("0.3"), Decimal("0.7")),
        deflated_sharpe=Decimal("0.97"),
        n_trials=9,
    )
    md = mod.render_markdown([report], data_window="2022-01..2026-05")
    assert "OOS honesty caveat" in md
    assert "Non-backtestable risk register" in md
    assert "fUST_a30" in md
    assert "Selection bias" in md
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_oos_profitability.py -v`
Expected: FAIL — module not found / attributes missing.

- [ ] **Step 3: Write the script**

```python
# backend_py/scripts/run_oos_profitability.py
"""Canary OOS profitability characterization (backlog #4, reframed).

Runs the deployed canary config (MeanReversion x fUST x {a30,p2}) over rolling
1-month OOS windows vs the AlwaysFRR passive benchmark, and writes a research doc
with bootstrap CIs + a selection-bias deflated-Sharpe check.

Spec:  docs/superpowers/specs/2026-05-28-canary-oos-profitability-validation-design.md
Plan:  docs/superpowers/plans/2026-05-28-canary-oos-profitability.md

Usage:
    cd backend_py
    uv run python scripts/run_oos_profitability.py \\
        --output docs/research/2026-05-28-canary-oos-profitability.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import (
    ActiveReturnSummary,
    OosSummary,
    WindowOutcome,
    active_return_summary,
    bootstrap_ci,
    deflated_sharpe,
    sharpe_skew_kurt,
    summarize_oos,
    _percentile,
)
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("run_oos_profitability")

START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
END_MTS = int(datetime.now(UTC).timestamp() * 1000)
# Configs tried per cell in the Phase 3b sweep that could have been selected:
# MeanReversion grid (2 ema_span x 3 threshold_sigma = 6) + RatePercentile (3) = 9.
DEFAULT_N_TRIALS = 9
LINEAR_CONFIG = BacktestConfig(fill_model="linear")


@dataclass(frozen=True)
class CanaryCell:
    strategy: str
    symbol: str
    period_agg: str
    timeframe: str
    threshold_sigma: Decimal
    ratio_sigma: Decimal
    ema_alpha: Decimal

    @property
    def label(self) -> str:
        return f"{self.symbol}_{self.period_agg}"


# Mirrors configs/cells.canary.yaml (single source of truth for deployed params).
CANARY_CELLS: list[CanaryCell] = [
    CanaryCell("mean_reversion", "fUST", "a30", "1h",
               Decimal("1.0"), Decimal("0.9915"), Decimal("0.01183")),
    CanaryCell("mean_reversion", "fUST", "p2", "1h",
               Decimal("1.0"), Decimal("0.9543"), Decimal("0.01183")),
]


@dataclass(frozen=True)
class CellReport:
    cell_label: str
    n_windows: int
    strat_summary: OosSummary
    base_summary: OosSummary
    active: ActiveReturnSummary
    median_ci: tuple[Decimal, Decimal]
    deflated_sharpe: Decimal
    n_trials: int


def alpha_to_ema_span(alpha: Decimal) -> int:
    """Invert _alpha = 2/(ema_span+1) used by MeanReversionStrategy. 0.01183 -> 168."""
    if alpha <= 0:
        raise ValueError(f"alpha_to_ema_span: alpha must be > 0, got {alpha}")
    return int((Decimal("2") / alpha - Decimal("1")).to_integral_value(rounding="ROUND_HALF_UP"))


def _outcome(result, month_mts: int) -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts,
        net_monthly=result.net_monthly_return_pct,
        n_trades=result.n_trades,
        fill_rate=result.fill_rate,
    )


def build_cell_report(
    cell: CanaryCell,
    strat_outcomes: list[WindowOutcome],
    base_outcomes: list[WindowOutcome],
    n_trials: int,
) -> CellReport:
    """Pure: turn per-window outcomes into a full CellReport."""
    strat_summary = summarize_oos(strat_outcomes)
    base_summary = summarize_oos(base_outcomes)
    active = active_return_summary(strat_outcomes, base_outcomes)

    monthly = [o.net_monthly for o in strat_outcomes]
    median_ci = bootstrap_ci(
        monthly, lambda vs: _percentile(sorted(vs), Decimal("0.5")), seed=20260528
    )

    sr, skew, kurt = sharpe_skew_kurt([m / Decimal("100") for m in monthly])
    dsr = deflated_sharpe(
        sr, n_trials=n_trials, n_obs=len(monthly), skew=skew, kurtosis=kurt
    )
    return CellReport(
        cell_label=cell.label,
        n_windows=len(strat_outcomes),
        strat_summary=strat_summary,
        base_summary=base_summary,
        active=active,
        median_ci=median_ci,
        deflated_sharpe=dsr,
        n_trials=n_trials,
    )


def render_markdown(reports: list[CellReport], *, data_window: str) -> str:
    """Pure: render the research doc."""
    lines: list[str] = []
    lines.append("# Canary OOS Profitability — fUST MeanReversion (a30, p2)\n")
    lines.append(f"**Run date**: {datetime.now(UTC).isoformat()}")
    lines.append(f"**Data window**: {data_window}")
    lines.append("**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)")
    lines.append("**Fill model**: linear (deterministic) — see methodology.\n")

    lines.append("## TL;DR\n")
    for r in reports:
        s, b = r.strat_summary, r.base_summary
        lines.append(
            f"- **{r.cell_label}** ({r.n_windows} months): strat median "
            f"{s.median_monthly:.4f}%/mo (annualized {s.annualized_pct:.2f}%), "
            f"baseline {b.median_monthly:.4f}%/mo; active median "
            f"{r.active.median_active:.4f}%/mo, IR {r.active.information_ratio}; "
            f"worst-month {s.worst_monthly:.4f}%, idle {s.idle_rate:.2%}; "
            f"deflated-Sharpe {r.deflated_sharpe:.4f}."
        )
    lines.append("")

    lines.append("## OOS honesty caveat\n")
    lines.append(
        "Deployed params were chosen by a sweep over this same 2022–2026 history, so "
        "these per-month returns are **in-sample to the parameter-selection process** — "
        "an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section "
        "quantifies the selection-bias haircut. The only true out-of-sample test is the "
        "live canary itself.\n"
    )

    for r in reports:
        s, b = r.strat_summary, r.base_summary
        lines.append(f"## Cell {r.cell_label}\n")
        lines.append("| Metric | Strategy | Baseline (AlwaysFRR) |")
        lines.append("|---|---|---|")
        lines.append(f"| median monthly % | {s.median_monthly:.4f} | {b.median_monthly:.4f} |")
        lines.append(f"| p25 monthly % | {s.p25_monthly:.4f} | {b.p25_monthly:.4f} |")
        lines.append(f"| worst month % | {s.worst_monthly:.4f} | {b.worst_monthly:.4f} |")
        lines.append(f"| best month % | {s.best_monthly:.4f} | {b.best_monthly:.4f} |")
        lines.append(f"| annualized % | {s.annualized_pct:.2f} | {b.annualized_pct:.2f} |")
        lines.append(f"| Sortino (monthly) | {s.sortino} | {b.sortino} |")
        lines.append(f"| idle rate | {s.idle_rate:.2%} | {b.idle_rate:.2%} |")
        lines.append(f"| mean fill rate | {s.mean_fill_rate:.4f} | {b.mean_fill_rate:.4f} |")
        lines.append("")
        lines.append(
            f"**Strategy median monthly 95% CI (bootstrap):** "
            f"[{r.median_ci[0]:.4f}%, {r.median_ci[1]:.4f}%]\n"
        )
        lines.append("### Active return vs passive\n")
        lines.append(
            f"- median active: {r.active.median_active:.4f}%/mo; "
            f"mean active: {r.active.mean_active:.4f}%/mo\n"
            f"- information ratio: {r.active.information_ratio}\n"
            f"- months strategy > baseline: {r.active.pct_months_outperform:.2%}\n"
        )
        lines.append("### Selection bias\n")
        lines.append(
            f"- configs tried (n_trials): {r.n_trials}; observations: {r.n_windows}\n"
            f"- **deflated Sharpe: {r.deflated_sharpe:.4f}** "
            f"(>0.95 = edge survives selection-bias deflation)\n"
        )

    lines.append("## Non-backtestable risk register\n")
    lines.append(
        "The catastrophic risk for a lending bot is **platform/credit/liquidity tail** "
        "(Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses "
        "it — it is mitigated by the **position cap ($450)** and not lending the full "
        "balance, NOT by this report. Treat these numbers as alpha characterization only.\n"
    )
    return "\n".join(lines)


def _report_to_json(reports: list[CellReport]) -> dict:
    def summ(s: OosSummary) -> dict:
        return {k: str(v) for k, v in s.__dict__.items()}
    return {
        "reports": [
            {
                "cell": r.cell_label,
                "n_windows": r.n_windows,
                "strategy": summ(r.strat_summary),
                "baseline": summ(r.base_summary),
                "active": {k: str(v) for k, v in r.active.__dict__.items()},
                "median_ci": [str(r.median_ci[0]), str(r.median_ci[1])],
                "deflated_sharpe": str(r.deflated_sharpe),
                "n_trials": r.n_trials,
            }
            for r in reports
        ]
    }


async def _run_cell(session, cell: CanaryCell, n_trials: int) -> CellReport:
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=END_MTS,
    )
    if not candles:
        raise SystemExit(f"No candles for {cell.label}; run scripts/backfill_candles.py")
    windows = compute_wfo_windows(candles)
    ema_span = alpha_to_ema_span(cell.ema_alpha)

    strat_outcomes: list[WindowOutcome] = []
    base_outcomes: list[WindowOutcome] = []
    for w in windows:
        slice_ = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
        strat = MeanReversionStrategy(
            ema_span=ema_span, threshold_sigma=cell.threshold_sigma, ratio_sigma=cell.ratio_sigma
        )
        base = AlwaysFRRStrategy(period_days=2)
        rs = run_backtest(slice_, strat, LINEAR_CONFIG, w.test_start_mts, w.test_end_mts)
        rb = run_backtest(slice_, base, LINEAR_CONFIG, w.test_start_mts, w.test_end_mts)
        strat_outcomes.append(_outcome(rs, w.test_start_mts))
        base_outcomes.append(_outcome(rb, w.test_start_mts))

    logger.info("%s: %d windows", cell.label, len(windows))
    return build_cell_report(cell, strat_outcomes, base_outcomes, n_trials)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown output path (.json sibling auto)")
    parser.add_argument("--n-trials", type=int, default=DEFAULT_N_TRIALS)
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    reports: list[CellReport] = []
    async with session_scope(factory) as session:
        for cell in CANARY_CELLS:
            reports.append(await _run_cell(session, cell, args.n_trials))
    await engine.dispose()

    data_window = f"{datetime.fromtimestamp(START_MTS/1000, UTC):%Y-%m} .. now"
    md = render_markdown(reports, data_window=data_window)
    out = Path(args.output)
    out.write_text(md)
    out.with_suffix(".json").write_text(json.dumps(_report_to_json(reports), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

> **Note on `db`/`Settings` bootstrap:** mirror `scripts/run_phase3b_wfo_matrix.py` exactly. If `make_engine`/`make_session_factory`/`session_scope`/`Settings()` differ in signature there (e.g. `Settings.load()` or `session_scope(factory)` vs `session_scope(engine)`), copy that script's exact usage. Verify by opening `scripts/run_phase3b_wfo_matrix.py` before running.

- [ ] **Step 4: Run helper tests to verify pass**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_oos_profitability.py -v`
Expected: PASS. (Pure helpers only — no DB.)

- [ ] **Step 5: Quality gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ scripts/run_oos_profitability.py && uv run ruff check`
Expected: PASS. Fix the `_percentile` private-import lint (add `# noqa` or expose it) and any mypy `result`-typing (annotate `_outcome(result: BacktestResult, ...)` and import `BacktestResult`) inline.

- [ ] **Step 6: Commit**

```bash
git add backend_py/scripts/run_oos_profitability.py backend_py/tests/scripts/test_run_oos_profitability.py
git commit -m "✨ Feat: run_oos_profitability script — canary OOS characterization"
```

---

## Task 6: Strategy-parity verification (Risk #1 — gate before trusting numbers)

**Goal:** Confirm the backtest `MeanReversionStrategy` and the **live marketfeed** MeanReversion produce the same lend/pause decision for the deployed params, or the validation does not reflect what is deployed.

**Files:**
- Read: `backend_py/src/bfx_funding_bot/modules/marketfeed/` (the live strategy + cell config consumer)
- Read: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py`
- Possibly create: `backend_py/tests/modules/backtest/test_mr_parity.py`

- [ ] **Step 1: Locate the live MR implementation**

Run: `cd backend_py && grep -rn "ema_alpha\|threshold_sigma\|ratio_sigma\|lower_band\|deviation" src/bfx_funding_bot/modules/marketfeed/ src/bfx_funding_bot/modules/strategy* 2>/dev/null`
Identify the live decision function and how it consumes `ema_alpha`, `threshold_sigma`, `ratio_sigma`.

- [ ] **Step 2: Compare the decision rule**

Confirm both implement: pause iff `(close - ema)/ema < -threshold_sigma * ratio_sigma`, with EMA recursion `ema = alpha*close + (1-alpha)*ema`, `alpha = 2/(ema_span+1) = 0.01183`. Note any divergence (e.g. live uses a different band, an extra spike filter, or `>=` vs `>`).

- [ ] **Step 3: Record the finding**

- If **equivalent**: add a one-paragraph note to the research doc's methodology ("backtest MR verified semantically equivalent to live marketfeed MR for deployed params") and proceed.
- If **divergent**: write a parity test `test_mr_parity.py` feeding an identical candle series to both and asserting identical decisions; if it fails, STOP and report the divergence to Will before running Task 7 — the numbers would not reflect the deployed strategy.

- [ ] **Step 4: Commit (if a parity test or note was added)**

```bash
git add -A && git commit -m "✅ Test: verify backtest MR == live marketfeed MR (deployed params)"
```

---

## Task 7: Run + produce the research doc + present

**Files:**
- Create: `docs/research/2026-05-28-canary-oos-profitability.md` + `.json` (generated)

- [ ] **Step 1: Ensure local Neon credentials are fresh**

The `.env` Neon password rotates. If the run fails to connect, refresh via Neon MCP `get_connection_string` and update `backend_py/.env` (symlink to repo-root `.env`). Project: `lingering-resonance-64910611`.

- [ ] **Step 2: Run the script (integration — hits Neon)**

Run:
```bash
cd backend_py && uv run python scripts/run_oos_profitability.py \
    --output ../docs/research/2026-05-28-canary-oos-profitability.md
```
Expected: logs "fUST_a30: ~49 windows", "fUST_p2: ~49 windows", "wrote ...md and ...json", exit 0.

- [ ] **Step 3: Sanity-check the output**

Open the generated md. Verify: both cells present; `worst_monthly >= 0` for both arms (confirms the lending-monotonic property); idle_rate plausible; if `ratio_sigma≈0.99` makes MR near-passive, active median ≈ 0 and idle_rate ≈ baseline's (Risk #2 surfaced). The doc must contain the OOS-caveat and risk-register sections.

- [ ] **Step 4: Commit the deliverable**

```bash
git add docs/research/2026-05-28-canary-oos-profitability.md docs/research/2026-05-28-canary-oos-profitability.json
git commit -m "📝 Docs: canary OOS profitability results (backlog #4)"
```

- [ ] **Step 5: Present to Will**

Summarize: realized median monthly + annualized per cell, active-vs-passive (is the selectivity worth it?), worst-month, idle rate, deflated Sharpe (does the edge survive?), and the Risk #2 finding (is the deployed config near-passive?). Then ask whether to (a) set a hard gate anchored to these numbers, (b) re-examine the deployed params, or (c) scale the cap. **Do not** invent a threshold — recommend based on the observed baseline + worst-month.

---

## Self-Review

**Spec coverage:**
- OOS rolling eval, fixed deployed params, both canary cells → Task 5 `_run_cell`. ✓
- Passive AlwaysFRR benchmark → Task 5 (base arm), Task 2 (active return). ✓
- Yield-native metrics (median/p25/worst/annualized, idle, fill, Sortino) → Task 1. ✓
- Active return + information ratio → Task 2. ✓
- Bootstrap CIs → Task 3, wired in Task 5 `build_cell_report`. ✓
- Selection-bias deflated Sharpe → Task 4, wired Task 5. ✓
- OOS honesty caveat + non-backtestable risk register in output → Task 5 `render_markdown`, checked in Task 7 Step 3. ✓
- Strategy-parity (Risk #1) → Task 6. ✓
- No hard gate this pass; numbers-driven recommendation → Task 7 Step 5. ✓
- Out-of-scope (fUSD, RatePercentile, p30, engine downside modeling, Tier-3) → not implemented. ✓

**Placeholder scan:** No TBD/TODO. The only deferred verification is the `db`/`Settings` bootstrap signature (Task 5 note) and live-MR location (Task 6 Step 1) — both are "open the named file and mirror it" instructions with exact grep commands, not vague placeholders.

**Type consistency:** `WindowOutcome` fields (`month_mts, net_monthly, n_trades, fill_rate`) used identically in Tasks 1, 2, 5. `OosSummary` / `ActiveReturnSummary` field names match between dataclass defs (Tasks 1/2) and the `render_markdown` / test usage (Task 5). `deflated_sharpe` and `bootstrap_ci` are keyword-only past their first positional arg in both def and call sites. `compute_sortino` fed fractions (÷100) consistently in Task 1 and Task 5.

**Known follow-up (not a plan gap):** `from ... import _percentile` (private) is used by the script for the bootstrap median stat_fn; Task 5 Step 5 flags the ruff fix. Acceptable — the alternative is exposing a public `median()` wrapper; either is fine for the implementer.
