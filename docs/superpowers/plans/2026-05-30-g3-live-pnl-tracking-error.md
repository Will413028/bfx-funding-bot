# G3 Live P&L Tracking-Error / Active-vs-Passive Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live validation report that confirms the deployed MeanReversion canary config produces the backtest-predicted active spread vs passive AlwaysFRR, on real fills, with a four-state human-read verdict.

**Architecture:** A pure, I/O-free attribution module (`modules/live_validation/live_attribution.py`) turns live fill/FRR/checkpoint records into `WindowOutcome` lists (strat + passive arms) and a verdict, reusing the existing `modules/backtest/oos_profitability.py` metric functions unchanged. A thin script (`scripts/run_g3_live_validation.py`) loads from Neon (`event_log` fills, `funding_stats` FRR, reconcile checkpoints), runs the module, and writes a markdown + JSON report. Both arms normalize to a fixed capital budget C (the canary cap); passive return reduces to `mean_frr × days` (capital cancels), active to `Σ(size·rate·dur)/C`.

**Tech Stack:** Python 3.13, Decimal arithmetic, SQLAlchemy 2.0 async, pytest. All commands run from `backend_py/` (uv-managed py3.13).

**Spec:** `docs/superpowers/specs/2026-05-30-g3-live-pnl-tracking-error-design.md`

**Reused (do not modify):** `modules/backtest/oos_profitability.py` — `WindowOutcome`, `active_return_summary`, `paired_active_returns`, `bootstrap_ci`, `percentile`. `modules/funding_stats/repository.py` — `get_in_range`. Event read pattern: `modules/admin/pg_event_log_query.py`.

---

## File Structure

- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/__init__.py` (empty)
- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py` — pure attribution + windowing + anchors + verdict
- Create: `backend_py/tests/modules/live_validation/__init__.py` (empty)
- Create: `backend_py/tests/modules/live_validation/test_live_attribution.py` — unit tests
- Create: `backend_py/scripts/run_g3_live_validation.py` — thin orchestration
- Create: `backend_py/tests/scripts/test_run_g3_live_validation.py` — integration smoke

All `WindowOutcome` instances use the existing frozen dataclass: `WindowOutcome(month_mts: int, net_monthly: Decimal, n_trades: int, fill_rate: Decimal)`. `net_monthly` carries the window return in **percent**.

---

### Task 1: Module scaffold + dataclasses + `cell_period_days`

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Create: `backend_py/tests/modules/live_validation/__init__.py`
- Create: `backend_py/tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Create the two empty `__init__.py` files**

```bash
cd backend_py
touch src/bfx_funding_bot/modules/live_validation/__init__.py
touch tests/modules/live_validation/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `backend_py/tests/modules/live_validation/test_live_attribution.py`:

```python
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    FrrPoint,
    cell_period_days,
)


def test_cell_period_days_p2_is_two():
    assert cell_period_days("p2", Decimal("30")) == Decimal("2")


def test_cell_period_days_a30_uses_avg_period():
    assert cell_period_days("a30", Decimal("27.5")) == Decimal("27.5")


def test_cell_period_days_unknown_raises():
    with pytest.raises(ValueError, match="unknown period_agg"):
        cell_period_days("p7", Decimal("30"))


def test_fillrecord_is_frozen():
    f = FillRecord(
        venue_offer_id="1",
        fill_ts_ms=0,
        size_usdt=Decimal("100"),
        rate=Decimal("0.0003"),
        period_days=Decimal("2"),
        release_ts_ms=None,
    )
    with pytest.raises(Exception):
        f.size_usdt = Decimal("200")  # type: ignore[misc]


def test_frrpoint_is_frozen():
    p = FrrPoint(mts=0, frr=Decimal("0.0002"), avg_period=Decimal("30"))
    assert p.frr == Decimal("0.0002")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError` (module/symbols not defined).

- [ ] **Step 4: Write minimal implementation**

Create `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/modules/live_validation/ tests/modules/live_validation/
git commit -m "✅ Test: G3 live_attribution scaffold — FillRecord/FrrPoint/cell_period_days"
```

---

### Task 2: `weekly_window_bounds`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write the failing test** (append to the test file)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    weekly_window_bounds,
)

WEEK = 7 * 24 * 60 * 60 * 1000


def test_weekly_window_bounds_exact_two_weeks():
    bounds = weekly_window_bounds(0, 2 * WEEK)
    assert bounds == [(0, WEEK), (WEEK, 2 * WEEK)]


def test_weekly_window_bounds_partial_trailing_week():
    bounds = weekly_window_bounds(0, WEEK + 100)
    assert bounds == [(0, WEEK), (WEEK, WEEK + 100)]


def test_weekly_window_bounds_single_short_span():
    bounds = weekly_window_bounds(1000, 5000)
    assert bounds == [(1000, 5000)]


def test_weekly_window_bounds_empty_when_end_le_start():
    assert weekly_window_bounds(5000, 5000) == []
    assert weekly_window_bounds(5000, 4000) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k weekly -v`
Expected: FAIL with `ImportError` (`weekly_window_bounds` not defined).

- [ ] **Step 3: Write minimal implementation** (append to `live_attribution.py`)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k weekly -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 weekly_window_bounds"
```

---

### Task 3: `attribute_passive`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write the failing test** (append)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    attribute_passive,
)


def test_attribute_passive_single_full_week():
    # one FRR point of 0.0003/day over a 7-day window -> 0.0003 * 7 * 100 = 0.21%
    pts = [FrrPoint(mts=1000, frr=Decimal("0.0003"), avg_period=Decimal("30"))]
    bounds = [(0, WEEK)]
    out = attribute_passive(pts, window_bounds=bounds)
    assert len(out) == 1
    assert out[0].month_mts == 0
    assert out[0].n_trades == 1
    assert out[0].net_monthly == Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")


def test_attribute_passive_averages_frr_in_window():
    pts = [
        FrrPoint(mts=10, frr=Decimal("0.0002"), avg_period=Decimal("30")),
        FrrPoint(mts=20, frr=Decimal("0.0004"), avg_period=Decimal("30")),
    ]
    out = attribute_passive(pts, window_bounds=[(0, WEEK)])
    # mean frr = 0.0003
    expected = Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")
    assert out[0].net_monthly == expected
    assert out[0].fill_rate == Decimal("0.0003")


def test_attribute_passive_zero_frr_points_in_window():
    out = attribute_passive([], window_bounds=[(0, WEEK)])
    assert out[0].net_monthly == Decimal("0")
    assert out[0].n_trades == 0
    assert out[0].fill_rate == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k passive -v`
Expected: FAIL (`attribute_passive` not defined).

- [ ] **Step 3: Write minimal implementation** (append; import `WindowOutcome` at top of file)

Add to the imports block at the top of `live_attribution.py`:

```python
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
```

Append the function:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k passive -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 attribute_passive (AlwaysFRR arm)"
```

---

### Task 4: `attribute_active` (with duration cap)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write the failing test** (append)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    attribute_active,
)

C = Decimal("570")


def _fill(ts, size, rate, period, release=None):
    return FillRecord(
        venue_offer_id=str(ts),
        fill_ts_ms=ts,
        size_usdt=Decimal(size),
        rate=Decimal(rate),
        period_days=Decimal(period),
        release_ts_ms=release,
    )


def test_attribute_active_held_to_term():
    # one fill: 570 @ 0.0003/day for 2 days -> interest 0.342 -> /570*100 = 0.06%
    fills = [_fill(1000, "570", "0.0003", "2")]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    assert out[0].n_trades == 1
    expected = Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")
    assert out[0].net_monthly == expected
    assert out[0].fill_rate == Decimal("0.0003")


def test_attribute_active_release_caps_duration():
    # period 2 days but released after 1 day -> duration capped to 1 day
    one_day = 24 * 60 * 60 * 1000
    fills = [_fill(0, "570", "0.0003", "2", release=one_day)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    expected = Decimal("570") * Decimal("0.0003") * Decimal("1") / C * Decimal("100")
    assert out[0].net_monthly == expected


def test_attribute_active_release_longer_than_period_uses_period():
    # released after 5 days but period is 2 -> use period (held-to-term proxy)
    five_days = 5 * 24 * 60 * 60 * 1000
    fills = [_fill(0, "570", "0.0003", "2", release=five_days)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    expected = Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")
    assert out[0].net_monthly == expected


def test_attribute_active_multiple_fills_one_window():
    fills = [_fill(100, "200", "0.0003", "2"), _fill(200, "300", "0.0005", "2")]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    interest = (
        Decimal("200") * Decimal("0.0003") * Decimal("2")
        + Decimal("300") * Decimal("0.0005") * Decimal("2")
    )
    assert out[0].net_monthly == interest / C * Decimal("100")
    assert out[0].n_trades == 2
    assert out[0].fill_rate == (Decimal("0.0003") + Decimal("0.0005")) / Decimal("2")


def test_attribute_active_zero_fill_window_is_idle():
    out = attribute_active([], capital=C, window_bounds=[(0, WEEK)])
    assert out[0].net_monthly == Decimal("0")
    assert out[0].n_trades == 0
    assert out[0].fill_rate == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k active -v`
Expected: FAIL (`attribute_active` not defined).

- [ ] **Step 3: Write minimal implementation** (append)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k active -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 attribute_active with release-capped duration"
```

---

### Task 5: Deployment anchor

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

The deployment anchor compares the attribution's assumed deployed principal at each
reconcile checkpoint against the venue-observed `realized` principal at that checkpoint.
Both are single scalars summarized over the validation span: `attributed_deployed` =
mean active principal the attribution assumes was open; `observed_realized` = mean venue
realized across checkpoints. Divergence is relative; tolerance default 5%.

- [ ] **Step 1: Write the failing test** (append)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    DeploymentAnchorResult,
    check_deployment_anchor,
)


def test_deployment_anchor_within_tolerance():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("310"),
        tol=Decimal("0.05"),
    )
    assert isinstance(r, DeploymentAnchorResult)
    assert r.within_tolerance is True
    assert r.relative_divergence == abs(Decimal("300") - Decimal("310")) / Decimal("310")


def test_deployment_anchor_beyond_tolerance():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("100"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is False


def test_deployment_anchor_zero_observed_is_within_when_attributed_zero():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("0"),
        observed_realized=Decimal("0"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is True
    assert r.relative_divergence == Decimal("0")


def test_deployment_anchor_zero_observed_nonzero_attributed_diverges():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("50"),
        observed_realized=Decimal("0"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k deployment_anchor -v`
Expected: FAIL (`DeploymentAnchorResult` / `check_deployment_anchor` not defined).

- [ ] **Step 3: Write minimal implementation** (append)

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k deployment_anchor -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 deployment anchor (attributed vs venue realized)"
```

---

### Task 6: NAV anchor (best-effort)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

The NAV anchor cross-checks `ΔNAV ≈ Σ attributed interest` when NAV samples are
available (process uptime spanned the window). When `nav_delta is None`, the anchor
reports unavailable and does not fail the verdict. Tolerance is relative to the
attributed interest, default 10% (looser — NAV is confounded by deposit/fee timing).

- [ ] **Step 1: Write the failing test** (append)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    NavAnchorResult,
    check_nav_anchor,
)


def test_nav_anchor_unavailable():
    r = check_nav_anchor(
        nav_delta=None, attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert isinstance(r, NavAnchorResult)
    assert r.available is False
    assert r.within_tolerance is True  # unavailable does not fail the verdict


def test_nav_anchor_within_tolerance():
    r = check_nav_anchor(
        nav_delta=Decimal("1.05"), attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is True


def test_nav_anchor_beyond_tolerance():
    r = check_nav_anchor(
        nav_delta=Decimal("2.0"), attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is False


def test_nav_anchor_zero_attributed_zero_delta_within():
    r = check_nav_anchor(
        nav_delta=Decimal("0"), attributed_interest=Decimal("0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k nav_anchor -v`
Expected: FAIL (`NavAnchorResult` / `check_nav_anchor` not defined).

- [ ] **Step 3: Write minimal implementation** (append)

```python
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
    """Best-effort ΔNAV vs Σ attributed interest. Unavailable -> within_tolerance True."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k nav_anchor -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 NAV anchor (best-effort ΔNAV reconciliation)"
```

---

### Task 7: `G3Verdict` + `decide_verdict`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py`
- Test: `backend_py/tests/modules/live_validation/test_live_attribution.py`

The decision table (evaluated in this order):
1. Either anchor beyond tolerance → `UNRELIABLE`.
2. `n_windows < min_windows` OR `total_capital_days < min_capital_days` → `INSUFFICIENT_DATA`.
3. `ci_hi < 0` → `FAIL` (significantly loses to passive).
4. `ci_lo > 0` → `PASS` (significantly beats passive).
5. Otherwise (CI straddles 0 with enough data) → `INSUFFICIENT_DATA` with reason "CI straddles 0".

- [ ] **Step 1: Write the failing test** (append)

```python
from bfx_funding_bot.modules.live_validation.live_attribution import (
    G3Verdict,
    VerdictState,
    decide_verdict,
)


def _kw(**over):
    base = dict(
        headline_active_spread=Decimal("0.06"),
        n_windows=10,
        total_capital_days=Decimal("4000"),
        ci_lo=Decimal("0.01"),
        ci_hi=Decimal("0.10"),
        deployment_anchor=check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        nav_anchor=check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        min_windows=8,
        min_capital_days=Decimal("3990"),  # C * 7 = 570 * 7
    )
    base.update(over)
    return base


def test_verdict_pass():
    v = decide_verdict(**_kw())
    assert isinstance(v, G3Verdict)
    assert v.state is VerdictState.PASS


def test_verdict_fail_when_ci_hi_negative():
    v = decide_verdict(**_kw(ci_lo=Decimal("-0.10"), ci_hi=Decimal("-0.01")))
    assert v.state is VerdictState.FAIL


def test_verdict_insufficient_when_few_windows():
    v = decide_verdict(**_kw(n_windows=7))
    assert v.state is VerdictState.INSUFFICIENT_DATA


def test_verdict_insufficient_when_low_capital_days():
    v = decide_verdict(**_kw(total_capital_days=Decimal("100")))
    assert v.state is VerdictState.INSUFFICIENT_DATA


def test_verdict_insufficient_when_ci_straddles_zero():
    v = decide_verdict(**_kw(ci_lo=Decimal("-0.02"), ci_hi=Decimal("0.05")))
    assert v.state is VerdictState.INSUFFICIENT_DATA
    assert "straddles" in " ".join(v.reasons).lower()


def test_verdict_unreliable_when_deployment_anchor_diverges():
    bad = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("50"),
        tol=Decimal("0.05"),
    )
    v = decide_verdict(**_kw(deployment_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE


def test_verdict_unreliable_when_nav_anchor_diverges():
    bad = check_nav_anchor(
        nav_delta=Decimal("5"), attributed_interest=Decimal("1"), tol=Decimal("0.1")
    )
    v = decide_verdict(**_kw(nav_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE


def test_verdict_unreliable_takes_priority_over_insufficient():
    bad = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("50"),
        tol=Decimal("0.05"),
    )
    v = decide_verdict(**_kw(n_windows=2, deployment_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k verdict -v`
Expected: FAIL (`VerdictState` / `G3Verdict` / `decide_verdict` not defined).

- [ ] **Step 3: Write minimal implementation** (append; add `from enum import Enum` to imports)

Add to the imports block at the top:

```python
from enum import Enum
```

Append:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k verdict -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Run the full module test + mypy + ruff**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/ -v && uv run mypy src/bfx_funding_bot/modules/live_validation/ && uv run ruff check src/bfx_funding_bot/modules/live_validation/`
Expected: all PASS / clean.

- [ ] **Step 6: Commit**

```bash
cd backend_py
git add -A && git commit -m "✅ Test: G3 decide_verdict four-state decision table"
```

---

### Task 8: Orchestration script + report rendering

**Files:**
- Create: `backend_py/scripts/run_g3_live_validation.py`
- Test: `backend_py/tests/scripts/test_run_g3_live_validation.py`

This task wires the pure module to Neon and renders the report. The pure rendering
function (`render_markdown`) is unit-testable without a DB; the data-loading path is
integration (marked `integration`, outside the commit gate). Follow the structure of
`scripts/run_oos_profitability.py` (argparse → async load → compute → write .md + .json).

**Cell resolution note:** `ORDER_FILL` payloads do not carry the cell (a30 vs p2). The
script resolves `period_days` per fill as follows: look up the nearest `FrrPoint` at/before
`fill_ts_ms` for `avg_period`; if the deployed cells share a symbol and the fill cannot be
attributed to a specific cell, use the **shorter** configured period (p2 → 2 days) as a
**conservative** duration (under-attributes active return → makes PASS harder, never easier).
Document this assumption in the report's honesty caveats. If a future event carries cell
identity, switch to exact per-cell periods.

- [ ] **Step 1: Write the failing test** (rendering only — pure, no DB)

Create `backend_py/tests/scripts/test_run_g3_live_validation.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    VerdictState,
    check_deployment_anchor,
    check_nav_anchor,
    decide_verdict,
)


def _verdict(state_kwargs):
    return decide_verdict(
        headline_active_spread=Decimal("0.06"),
        n_windows=10,
        total_capital_days=Decimal("4000"),
        ci_lo=Decimal("0.01"),
        ci_hi=Decimal("0.10"),
        deployment_anchor=check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        nav_anchor=check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        min_windows=8,
        min_capital_days=Decimal("3990"),
        **state_kwargs,
    )


def test_render_markdown_contains_verdict_and_headline():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({})
    md = render_markdown(verdict=v, data_window="2026-05-30..2026-06-30", n_fills=12)
    assert "PASS" in md
    assert "0.06" in md
    assert "G3 Live Validation" in md


def test_render_markdown_insufficient_data_states_caveat():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"n_windows": 3})
    assert v.state is VerdictState.INSUFFICIENT_DATA
    md = render_markdown(verdict=v, data_window="n/a", n_fills=0)
    assert "INSUFFICIENT_DATA" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_g3_live_validation.py -v`
Expected: FAIL (`scripts.run_g3_live_validation` not defined).

- [ ] **Step 3: Write the script**

Create `backend_py/scripts/run_g3_live_validation.py`:

```python
"""G3 — live active-vs-passive validation of the deployed canary MR config.

Loads live fills (event_log), FRR series (funding_stats), and reconcile checkpoints
from Neon; attributes active vs passive yield (modules/live_validation), reuses the
oos_profitability metrics, and writes a markdown + JSON report with a four-state verdict.

Run from backend_py/:  uv run python scripts/run_g3_live_validation.py --out docs/research/<date>-g3-live-validation.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.live_attribution import (
    G3Verdict,
)


def render_markdown(*, verdict: G3Verdict, data_window: str, n_fills: int) -> str:
    """Pure renderer — unit-testable without a DB."""
    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {data_window} | fills: {n_fills} | weekly windows: {verdict.n_windows}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline active spread (since inception): {verdict.headline_active_spread}%",
        f"- Active-spread 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        "## Honesty caveats",
        "- Held-to-term duration assumption (matured credits have no close event).",
        "- Fills attributed with the conservative shorter period when cell identity is absent.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.",
        "",
        "## Recommendation",
        "- PASS: live alpha confirmed; scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing fills; re-run after more weekly windows.",
        "- FAIL: deployed config does not beat passive live — investigate before scaling.",
        "- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.",
    ]
    return "\n".join(lines)


def _verdict_to_json(v: G3Verdict) -> dict[str, object]:
    return {
        "state": v.state.value,
        "headline_active_spread": str(v.headline_active_spread),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
    }


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output .md path")
    parser.add_argument("--capital", default="570", help="capital budget C (canary cap)")
    args = parser.parse_args()

    # Integration wiring (loads from Neon) is implemented in Step 5 below.
    # This guard keeps the module importable for the pure render test.
    from scripts._g3_loaders import build_verdict_from_neon  # local import

    verdict, data_window, n_fills = await build_verdict_from_neon(
        capital=Decimal(args.capital)
    )

    out = Path(args.out)
    out.write_text(render_markdown(verdict=verdict, data_window=data_window, n_fills=n_fills))
    out.with_suffix(".json").write_text(json.dumps(_verdict_to_json(verdict), indent=2))
    print(f"wrote {out} and {out.with_suffix('.json')}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the render test to verify it passes**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_g3_live_validation.py -v`
Expected: PASS (2 tests). The Neon loader is imported lazily inside `_amain`, so the render tests do not require it.

- [ ] **Step 5: Implement the Neon loader**

Create `backend_py/scripts/_g3_loaders.py` with `build_verdict_from_neon` that:
1. Opens an async session via the project's standard engine factory (see `run_oos_profitability.py` for the exact `make_async_engine_from_url` / settings import).
2. Queries `event_log` for `ORDER_FILL` + `RESERVATION_RELEASED` rows (account_id + deployment_environment scoped — mirror `pg_event_log_query.py`), deserializing payloads via `deserialize_event`; build `FillRecord`s (join release to fill by `venue_offer_id`; resolve `period_days` via `cell_period_days` using nearest `FrrPoint.avg_period`, conservative-shorter fallback).
3. Loads FRR via `funding_stats.repository.get_in_range(symbol="fUST", start_mts, end_mts)` → `FrrPoint`s.
4. Loads reconcile checkpoints for `observed_realized` (mean realized across checkpoints) — query `position_state` / `reconcile_observation` per `event_store/store.py`.
5. Builds `weekly_window_bounds`, calls `attribute_active` / `attribute_passive`, `paired_active_returns`, `bootstrap_ci` (lo, hi), computes headline (single full-span window), `total_capital_days = Σ size_i·duration_i / capital`, `attributed_deployed` (mean open principal), and the anchors.
6. Returns `(decide_verdict(...), data_window_str, n_fills)`.

Mark the integration test of this loader with `@pytest.mark.integration` so it stays out of the commit gate.

- [ ] **Step 6: Commit**

```bash
cd backend_py
git add scripts/run_g3_live_validation.py scripts/_g3_loaders.py tests/scripts/test_run_g3_live_validation.py
git commit -m "✨ Feat: G3 live validation script + report rendering"
```

---

### Task 9: Run against Neon + write the report

**Files:**
- Create: `backend_py/docs/research/<run-date>-g3-live-validation.md` (+ `.json`) — generated

- [ ] **Step 1: Run the full commit gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: all PASS / clean.

- [ ] **Step 2: Run the validation against Neon**

Run: `cd backend_py && uv run python scripts/run_g3_live_validation.py --out docs/research/$(date +%Y-%m-%d)-g3-live-validation.md`
Expected: writes `.md` + `.json`. Given the idle canary, the expected verdict is `INSUFFICIENT_DATA` with a clean headline (no error on thin/empty data).

- [ ] **Step 3: Read the report and present numbers to Will**

Surface the verdict, headline active spread, window count, and the vs-backtest comparison. Decide jointly whether to wait for more live data or (if PASS) plan scale-up sizing.

- [ ] **Step 4: Commit the generated report**

```bash
cd backend_py
git add docs/research/*-g3-live-validation.md docs/research/*-g3-live-validation.json
git commit -m "📝 Docs: G3 live validation report (first run)"
```

---

## Self-Review

**Spec coverage:**
- Rate-spread attribution (primary) → Tasks 3, 4. ✅
- Capital normalization to C → Tasks 3, 4 (capital param / cancels in passive). ✅
- Dual-tier output (headline + weekly, N≥8 gate) → Tasks 2, 7 (min_windows), 8/9 (headline). ✅
- Deployment anchor (durable) → Task 5. ✅
- NAV anchor (best-effort, unavailable-safe) → Task 6. ✅
- Four-state verdict → Task 7. ✅
- Reuse oos_profitability (active_return_summary / bootstrap_ci) → Task 8 loader. ✅
- Report format + honesty caveats → Task 8 render_markdown. ✅
- Sparse-data tolerance (clean INSUFFICIENT_DATA, no error) → Task 9 Step 2. ✅
- Deferred (source C, durable NAV, always-on, fUSD) → not implemented, per spec. ✅

**Placeholder scan:** Task 8 Step 5 (Neon loader) is described as numbered sub-steps rather than full code, because it is integration glue against project-specific session/engine factories that must be read from `run_oos_profitability.py` at implementation time; each sub-step names the exact module/function to use. All pure-logic tasks (1–7) and the pure renderer (8 Steps 1–4) have complete code. This is the one acceptable coarsening (integration wiring), explicitly flagged.

**Type consistency:** `WindowOutcome(month_mts, net_monthly, n_trades, fill_rate)` used consistently (Tasks 3, 4). `DeploymentAnchorResult.within_tolerance`, `NavAnchorResult.within_tolerance`, `G3Verdict.state: VerdictState` consistent across Tasks 5, 6, 7. `decide_verdict` keyword signature matches the Task 7 test and the Task 8 test's `_verdict` helper. `check_deployment_anchor` / `check_nav_anchor` keyword-only signatures consistent across Tasks 5, 6, 7, 8.
