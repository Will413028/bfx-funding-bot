# Adaptive-Period Band `(t1,t2)` Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sweep the AdaptivePeriod band edges `(t1,t2)` over 8 variants × 4 cells × disjoint windows, pick a deploy band by a robustness-over-performance procedure (paired difference test + disjoint-window replication + active-series DSR), and fold the result + `p_long=14` back into `param_grid_for_cell` — all offline, zero live impact.

**Architecture:** A pure `band_sweep` module (grid enumeration, period-path simulation, active-series DSR, paired band-vs-band difference CI, disjoint split, report build + render) reusing the existing OOS engine and stats primitives; a thin async driver script that fetches candles once, evaluates all 8 bands per cell, splits outcomes into disjoint windows post-hoc, and writes a research report. Then a decision task and a `param_grid` fold-back. No change to `engine.py`, `oos_eval.py`, or any live/canary path.

**Tech Stack:** Python 3.13, SQLAlchemy async (candle fetch), Decimal math, pytest. Run everything from `backend_py/`.

**Spec:** `docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md`

**Branch:** create `feat/adaptive-period-band-sweep` off `main` (isolated worktree recommended via `superpowers:using-git-worktrees`). After worktree rebuild, re-link `.env`: `ln -sf ../.env backend_py/.env`.

---

## File Structure

- **Create** `backend_py/src/bfx_funding_bot/modules/backtest/band_sweep.py` — all pure logic (grid, period path, active DSR, paired-diff CI, disjoint split, report dataclass, build, render). Importable + unit-tested.
- **Create** `backend_py/scripts/run_adaptive_band_sweep.py` — async orchestration only (argparse, candle fetch, loop cells×bands, call `band_sweep`, write `.md` + `.json`). Mirrors `run_oos_profitability.py` bootstrap.
- **Create** `backend_py/tests/modules/backtest/test_band_sweep.py` — unit tests for the pure module.
- **Modify** `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py:104-125` — `param_grid_for_cell` fold-back (Task 10).
- **Modify** `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py:201-218` — UPDATE `test_param_grid_for_cell_from_eda` (Task 10).
- **Output** `docs/research/2026-06-04-adaptive-period-band-sweep.md` (+ `.json`) — the sweep report (Task 9).

**Tasks 1-8 are pure machinery (complete code below). Task 9 runs it. Task 10's band value is the *output* of Task 9's decision — Task 10 supplies the fold-back mechanism; the chosen `(t1,t2)` is filled from Task 9's report.**

All commands run from `backend_py/`. Test command: `uv run pytest -m "not integration"`. Lint: `uv run mypy src/ && uv run ruff check`.

---

### Task 1: band_sweep module + grid enumeration

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/backtest/test_band_sweep.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.band_sweep import enumerate_bands


def test_enumerate_bands_is_8_strict_pairs() -> None:
    bands = enumerate_bands()
    assert len(bands) == 8
    # strict t1 < t2, no (1.5, 1.5)
    assert all(t1 < t2 for t1, t2 in bands)
    assert (Decimal("1.5"), Decimal("1.5")) not in bands
    # the deployed candidate is in-grid
    assert (Decimal("0.5"), Decimal("1.5")) in bands
    # exact set
    assert set(bands) == {
        (Decimal("0.5"), Decimal("1.5")), (Decimal("0.5"), Decimal("2.0")), (Decimal("0.5"), Decimal("2.5")),
        (Decimal("1.0"), Decimal("1.5")), (Decimal("1.0"), Decimal("2.0")), (Decimal("1.0"), Decimal("2.5")),
        (Decimal("1.5"), Decimal("2.0")), (Decimal("1.5"), Decimal("2.5")),
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py::test_enumerate_bands_is_8_strict_pairs -v`
Expected: FAIL with `ModuleNotFoundError: ... band_sweep`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/bfx_funding_bot/modules/backtest/band_sweep.py
"""Pure logic for the AdaptivePeriod band (t1,t2) sweep.

Reuses the OOS engine + stats primitives; adds the two new statistics the
shared build_cell_report does not compute (active-series DSR; paired
band-vs-band difference CI). No engine/oos_eval changes. See
docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md.
"""
from __future__ import annotations

from decimal import Decimal

_T1_VALUES = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]
_T2_VALUES = [Decimal("1.5"), Decimal("2.0"), Decimal("2.5")]


def enumerate_bands() -> list[tuple[Decimal, Decimal]]:
    """The 8 (t1, t2) variants, strict t1 < t2 (drops the (1.5,1.5) cell)."""
    return [(t1, t2) for t1 in _T1_VALUES for t2 in _T2_VALUES if t1 < t2]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py::test_enumerate_bands_is_8_strict_pairs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/backtest/band_sweep.py tests/modules/backtest/test_band_sweep.py
git commit -m "✨ Feat: band_sweep grid enumeration (8 (t1,t2) variants)"
```

---

### Task 2: period-path simulation (avg_period, p14-share)

`WindowOutcome` has no period field, so we re-derive the trade-level period distribution by mirroring the engine's observe→cooldown→decide loop (engine.py:104-127). We reuse `AdaptivePeriodStrategy` for the deviation→period logic (DRY) and lock it to the engine with a consistency test.

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to test_band_sweep.py
import math

from bfx_funding_bot.modules.backtest.band_sweep import simulate_period_path, period_profile
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(closes: list[str], symbol: str = "fUST") -> list[FundingCandle]:
    # hourly candles starting at an arbitrary epoch; only close/mts matter here
    base = 1_500_000_000_000
    return [
        FundingCandle(symbol=symbol, timeframe="1h", period_agg="a30",
                      mts=base + i * 3_600_000, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def test_simulate_period_path_matches_engine_trade_count() -> None:
    # A long, mildly varying series so several trades + cooldowns fire.
    closes = [str(Decimal("0.0003") + Decimal("0.0001") * Decimal((i % 7))) for i in range(400)]
    candles = _candles(closes)
    params = dict(ema_span=24, ratio_sigma=Decimal("0.40"), t1=Decimal("0.5"),
                  t2=Decimal("1.5"), p_mid=7, p_long=14)

    periods = simulate_period_path(candles, **params)

    strat = AdaptivePeriodStrategy(**params)
    rb = run_backtest(candles, strat, BacktestConfig(fill_model="linear"))
    assert len(periods) == rb.n_trades  # the cooldown loop is mirrored exactly
    assert all(p in (2, 7, 14) for p in periods)


def test_period_profile_avg_and_p14_share() -> None:
    profile = period_profile([14, 14, 2, 2], p_long=14)
    # avg = (14+14+2+2)/4 = 8 ; time-weighted p14 share = 28 / 32
    assert profile["avg_period"] == Decimal("8")
    assert profile["p14_share"] == (Decimal("28") / Decimal("32"))


def test_period_profile_empty() -> None:
    profile = period_profile([], p_long=14)
    assert profile["avg_period"] == Decimal("0")
    assert profile["p14_share"] == Decimal("0")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "period" -v`
Expected: FAIL (`ImportError: cannot import name 'simulate_period_path'`).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py
import math

from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_GAP_MINUTES_DEFAULT = 30  # matches BacktestConfig.gap_minutes default


def simulate_period_path(
    candles: list[FundingCandle],
    *,
    ema_span: int,
    ratio_sigma: Decimal,
    t1: Decimal,
    t2: Decimal,
    p_mid: int,
    p_long: int,
    gap_minutes: int = _GAP_MINUTES_DEFAULT,
) -> list[int]:
    """Trade-level period sequence, mirroring engine.run_backtest's
    observe -> cooldown-skip -> decide loop (engine.py:104-127) so the
    distribution matches the actual backtest trades. Full series, no record
    window (record window defaults to full span in the engine)."""
    strat = AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=ratio_sigma, t1=t1, t2=t2,
        p_mid=p_mid, p_long=p_long,
    )
    ordered = sorted(candles, key=lambda c: c.mts)
    gap_candles = math.ceil(gap_minutes / 60)
    cooldown_until_idx = -1
    periods: list[int] = []
    for i, candle in enumerate(ordered):
        strat.observe(candle)
        if i <= cooldown_until_idx:
            continue
        decision = strat.decide(candle)
        if decision is None:
            continue
        periods.append(decision.period_days)
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles
    return periods


def period_profile(periods: list[int], *, p_long: int) -> dict[str, Decimal]:
    """avg_period (trade-mean) + p14_share (time-weighted: fraction of total
    locked-days spent in p_long locks — the §3 tail-risk axis)."""
    if not periods:
        return {"avg_period": Decimal("0"), "p14_share": Decimal("0")}
    total_days = Decimal(sum(periods))
    long_days = Decimal(sum(p for p in periods if p == p_long))
    return {
        "avg_period": Decimal(sum(periods)) / Decimal(len(periods)),
        "p14_share": (long_days / total_days) if total_days > 0 else Decimal("0"),
    }
```

Note: `period_profile`'s args are keyword-only after the list — call as `period_profile(periods, p_long=14)`. Adjust the test calls if you used positional (they use keyword).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "period" -v`
Expected: PASS (3 tests). If `test_simulate_period_path_matches_engine_trade_count` fails on count, diff your loop against engine.py:104-127 — the bug is almost always the `observe` placement (must run every candle, before the cooldown check) or `gap_candles`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "✨ Feat: band_sweep period-path simulation (engine-consistent avg_period/p14-share)"
```

---

### Task 3: active-series deflated Sharpe

The shared `build_cell_report` DSR runs on the band-invariant bot-vs-idle *level*. The band selection optimizes the *active* edge vs always-2d, so DSR must run on `paired_active_returns`. Degenerate near-baseline bands (zero active variance) return `None` ("DSR undefined").

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to test_band_sweep.py
from bfx_funding_bot.modules.backtest.band_sweep import active_deflated_sharpe
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome


def _wo(month_mts: int, net: str) -> WindowOutcome:
    return WindowOutcome(month_mts=month_mts, net_monthly=Decimal(net), n_trades=1, fill_rate=Decimal("1"))


def test_active_dsr_strong_edge_is_high() -> None:
    # strat beats base by a steady ~0.5%/mo over 12 windows -> high DSR
    strat = [_wo(i, str(Decimal("1.0") + Decimal("0.01") * Decimal(i % 3))) for i in range(12)]
    base = [_wo(i, "0.5") for i in range(12)]
    dsr = active_deflated_sharpe(strat, base, n_trials=8)
    assert dsr is not None
    assert dsr > Decimal("0.5")


def test_active_dsr_zero_variance_is_none() -> None:
    # strat == base every window -> active series all 0 -> undefined
    strat = [_wo(i, "0.5") for i in range(12)]
    base = [_wo(i, "0.5") for i in range(12)]
    assert active_deflated_sharpe(strat, base, n_trials=8) is None


def test_active_dsr_too_few_windows_is_none() -> None:
    strat = [_wo(0, "1.0"), _wo(1, "1.0")]
    base = [_wo(0, "0.5"), _wo(1, "0.5")]
    assert active_deflated_sharpe(strat, base, n_trials=8) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "active_dsr" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py
from bfx_funding_bot.modules.backtest.oos_profitability import (
    WindowOutcome,
    deflated_sharpe,
    paired_active_returns,
    sharpe_skew_kurt,
)


def active_deflated_sharpe(
    strat: list[WindowOutcome],
    base: list[WindowOutcome],
    *,
    n_trials: int,
) -> Decimal | None:
    """Deflated Sharpe on the per-window ACTIVE series (strat - always-2d) —
    the statistic band selection optimizes. None if < 3 windows or the active
    series has zero variance (tight band ≈ baseline; DSR undefined)."""
    paired = paired_active_returns(strat, base)
    if len(paired) < 3:
        return None
    mean = sum(paired, Decimal("0")) / Decimal(len(paired))
    if all(p == mean for p in paired):  # zero variance -> Sharpe undefined
        return None
    sr, skew, kurt = sharpe_skew_kurt([p / Decimal("100") for p in paired])
    return deflated_sharpe(sr, n_trials=n_trials, n_obs=len(paired), skew=skew, kurtosis=kurt)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "active_dsr" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "✨ Feat: band_sweep active-series deflated Sharpe (+ degenerate guard)"
```

---

### Task 4: paired band-vs-band difference CI

Two bands run the same months vs the same baseline, so a marginal-CI-overlap test is pairing-blind and biased to "tied." Test the paired per-month difference instead. (Baseline cancels: `A_active − B_active = A_strat − B_strat`, so we difference the strat outcomes directly, aligned by `month_mts`.)

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to test_band_sweep.py
from bfx_funding_bot.modules.backtest.band_sweep import paired_difference_ci, is_tied


def test_paired_diff_strict_dominance_excludes_zero() -> None:
    a = [_wo(i, "1.0") for i in range(12)]   # A always +0.5 over B
    b = [_wo(i, "0.5") for i in range(12)]
    ci = paired_difference_ci(a, b)
    assert ci[0] > Decimal("0")              # whole CI above 0
    assert not is_tied(ci)


def test_paired_diff_identical_straddles_zero() -> None:
    a = [_wo(i, "0.7") for i in range(12)]
    b = [_wo(i, "0.7") for i in range(12)]
    ci = paired_difference_ci(a, b)
    assert is_tied(ci)                        # CI brackets 0


def test_paired_diff_aligns_by_month_mts() -> None:
    # b is shuffled / partially overlapping; only shared months count
    a = [_wo(0, "1.0"), _wo(1, "1.0"), _wo(2, "1.0")]
    b = [_wo(2, "0.5"), _wo(0, "0.5"), _wo(99, "0.0")]  # months 0,2 shared
    ci = paired_difference_ci(a, b)
    assert ci[0] > Decimal("0")               # 0.5 diff on the 2 shared months


def test_pairwise_tie_matrix_flags_winners_and_ties() -> None:
    from bfx_funding_bot.modules.backtest.band_sweep import pairwise_tie_matrix
    strong = [_wo(i, "1.0") for i in range(12)]
    weak = [_wo(i, "0.5") for i in range(12)]
    tie = [_wo(i, "1.0") for i in range(12)]   # identical to strong
    labeled = [
        ((Decimal("0.5"), Decimal("1.5")), strong),
        ((Decimal("1.0"), Decimal("2.0")), weak),
        ((Decimal("1.5"), Decimal("2.0")), tie),
    ]
    rows = pairwise_tie_matrix(labeled)
    assert len(rows) == 3                       # C(3,2)
    by_pair = {(r["band_a"], r["band_b"]): r for r in rows}
    assert by_pair[("(0.5,1.5)", "(1.0,2.0)")]["tied"] is False   # strong beats weak
    assert by_pair[("(0.5,1.5)", "(1.5,2.0)")]["tied"] is True    # strong == tie
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "paired_diff" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci

_PAIRED_DIFF_SEED = 20260604  # run date, fixed for reproducibility


def paired_difference_ci(
    a: list[WindowOutcome],
    b: list[WindowOutcome],
    *,
    seed: int = _PAIRED_DIFF_SEED,
) -> tuple[Decimal, Decimal]:
    """Bootstrap CI of the mean per-shared-month (A.net_monthly - B.net_monthly).
    Aligned by month_mts. Empty/degenerate -> (0, 0)."""
    b_by_mts = {o.month_mts: o.net_monthly for o in b}
    diffs = [o.net_monthly - b_by_mts[o.month_mts] for o in a if o.month_mts in b_by_mts]
    if len(diffs) < 2:
        return (Decimal("0"), Decimal("0"))
    return bootstrap_ci(diffs, lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)), seed=seed)


def is_tied(ci: tuple[Decimal, Decimal]) -> bool:
    """A pair is statistically indistinguishable iff its difference CI straddles 0."""
    return ci[0] <= Decimal("0") <= ci[1]


def pairwise_tie_matrix(
    labeled: list[tuple[tuple[Decimal, Decimal], list[WindowOutcome]]],
) -> list[dict[str, object]]:
    """For every band pair, the paired-difference CI of (A_strat − B_strat) and
    whether it straddles 0 (tied). Baseline cancels (A_active − B_active =
    A_strat − B_strat), so we difference the strat outcomes directly. Feeds the
    §7 step-3 null/tie decision over survivors."""
    rows: list[dict[str, object]] = []
    for i in range(len(labeled)):
        for j in range(i + 1, len(labeled)):
            (a_band, a_out), (b_band, b_out) = labeled[i], labeled[j]
            ci = paired_difference_ci(a_out, b_out)
            rows.append({
                "band_a": f"({a_band[0]},{a_band[1]})",
                "band_b": f"({b_band[0]},{b_band[1]})",
                "ci_lo": ci[0], "ci_hi": ci[1], "tied": is_tied(ci),
            })
    return rows
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "paired_diff or pairwise" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "✨ Feat: band_sweep paired difference CI + pairwise tie matrix (variance-correct)"
```

---

### Task 5: disjoint-window split

Rank consistency must use disjoint halves (pre-2022 vs 2022→), not nested recent⊂full.

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing test**

```python
# add to test_band_sweep.py
from bfx_funding_bot.modules.backtest.band_sweep import split_disjoint, SPLIT_MTS


def test_split_disjoint_partitions_by_2022() -> None:
    before = SPLIT_MTS - 86_400_000        # one day before boundary
    after = SPLIT_MTS + 86_400_000
    outcomes = [_wo(before, "1.0"), _wo(after, "2.0"), _wo(SPLIT_MTS, "3.0")]
    early, recent = split_disjoint(outcomes)
    assert [o.net_monthly for o in early] == [Decimal("1.0")]            # strictly before 2022
    assert {o.net_monthly for o in recent} == {Decimal("2.0"), Decimal("3.0")}  # >= 2022
    # non-overlapping + total preserved
    assert len(early) + len(recent) == len(outcomes)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "split_disjoint" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py (top-level, near imports)
from datetime import UTC, datetime

SPLIT_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)


def split_disjoint(
    outcomes: list[WindowOutcome],
    *,
    split_mts: int = SPLIT_MTS,
) -> tuple[list[WindowOutcome], list[WindowOutcome]]:
    """Partition outcomes into (early = test month < 2022, recent = >= 2022).
    Non-overlapping by construction — the corrected G2 robustness gate."""
    early = [o for o in outcomes if o.month_mts < split_mts]
    recent = [o for o in outcomes if o.month_mts >= split_mts]
    return early, recent
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "split_disjoint" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "✨ Feat: band_sweep disjoint-window split (pre-2022 vs 2022+)"
```

---

### Task 6: per-cell band report assembly

Combine the helpers into one structured result per (cell, band): median/mean active, mean÷median, best-month, win-rate, avg_period, p14-share, active-DSR, and early/recent median-active for the disjoint rank.

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing test**

```python
# add to test_band_sweep.py
from bfx_funding_bot.modules.backtest.band_sweep import BandResult, build_band_result


def test_build_band_result_fields() -> None:
    strat = [_wo(SPLIT_MTS - 86_400_000, "1.2"), _wo(SPLIT_MTS + 86_400_000, "0.8")]
    base = [_wo(SPLIT_MTS - 86_400_000, "0.5"), _wo(SPLIT_MTS + 86_400_000, "0.5")]
    r = build_band_result(
        t1=Decimal("0.5"), t2=Decimal("1.5"),
        strat_outcomes=strat, base_outcomes=base,
        periods=[14, 2, 2, 7], p_long=14, n_trials=8,
    )
    assert isinstance(r, BandResult)
    assert r.t1 == Decimal("0.5") and r.t2 == Decimal("1.5")
    assert r.n_windows == 2
    # active = strat - base = [0.7, 0.3] ; median 0.5, mean 0.5
    assert r.median_active == Decimal("0.5")
    assert r.avg_period == (Decimal("25") / Decimal("4"))   # (14+2+2+7)/4
    assert r.p14_share == (Decimal("14") / Decimal("25"))
    # early half has the pre-2022 window only
    assert r.early_median_active == Decimal("0.7")
    assert r.recent_median_active == Decimal("0.3")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "build_band_result" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py
from dataclasses import dataclass

from bfx_funding_bot.modules.backtest.oos_profitability import (
    active_return_summary,
    percentile,
)


@dataclass(frozen=True)
class BandResult:
    t1: Decimal
    t2: Decimal
    n_windows: int
    median_active: Decimal
    mean_active: Decimal
    mean_over_median: Decimal      # fat-tail proxy; 0 if median == 0
    best_month_active: Decimal
    win_rate: Decimal              # pct_months_outperform
    avg_period: Decimal
    p14_share: Decimal
    active_dsr: Decimal | None     # None = undefined (near-baseline / <3 windows)
    early_median_active: Decimal   # disjoint pre-2022 half
    recent_median_active: Decimal  # disjoint 2022+ half


def _median_active(strat: list[WindowOutcome], base: list[WindowOutcome]) -> Decimal:
    paired = paired_active_returns(strat, base)
    return percentile(paired, Decimal("0.5")) if paired else Decimal("0")


def build_band_result(
    *,
    t1: Decimal,
    t2: Decimal,
    strat_outcomes: list[WindowOutcome],
    base_outcomes: list[WindowOutcome],
    periods: list[int],
    p_long: int,
    n_trials: int,
) -> BandResult:
    active = active_return_summary(strat_outcomes, base_outcomes)
    paired = paired_active_returns(strat_outcomes, base_outcomes)
    profile = period_profile(periods, p_long=p_long)
    mean_over_median = (
        active.mean_active / active.median_active if active.median_active != 0 else Decimal("0")
    )
    # disjoint halves
    s_early, s_recent = split_disjoint(strat_outcomes)
    b_early, b_recent = split_disjoint(base_outcomes)
    return BandResult(
        t1=t1,
        t2=t2,
        n_windows=len(strat_outcomes),
        median_active=active.median_active,
        mean_active=active.mean_active,
        mean_over_median=mean_over_median,
        best_month_active=max(paired) if paired else Decimal("0"),
        win_rate=active.pct_months_outperform,
        avg_period=profile["avg_period"],
        p14_share=profile["p14_share"],
        active_dsr=active_deflated_sharpe(strat_outcomes, base_outcomes, n_trials=n_trials),
        early_median_active=_median_active(s_early, b_early),
        recent_median_active=_median_active(s_recent, b_recent),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "build_band_result" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "✨ Feat: band_sweep per-cell BandResult assembly"
```

---

### Task 7: render markdown report

Per-cell comparison table (both avg_period and p14-share surfaced), disjoint-window rank table, and the standing caveats. Paired-difference detail is computed in the driver over survivors; render shows the per-band table + ranks.

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py`

- [ ] **Step 1: Write the failing test**

```python
# add to test_band_sweep.py
from bfx_funding_bot.modules.backtest.band_sweep import render_cell_section


def test_render_cell_section_has_columns_and_caveats() -> None:
    r = build_band_result(
        t1=Decimal("0.5"), t2=Decimal("1.5"),
        strat_outcomes=[_wo(SPLIT_MTS + 1, "0.8")], base_outcomes=[_wo(SPLIT_MTS + 1, "0.5")],
        periods=[2, 14], p_long=14, n_trials=8,
    )
    md = render_cell_section("fUST_a30", [r])
    assert "fUST_a30" in md
    assert "avg_period" in md and "p14_share" in md          # both axes surfaced (§5)
    assert "0.5" in md and "1.5" in md                       # the band row
    assert "median active" in md.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "render_cell_section" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# add to band_sweep.py
def _fmt(d: Decimal | None, places: str = "0.0001") -> str:
    return "n/a" if d is None else str(d.quantize(Decimal(places)))


def render_cell_section(cell_label: str, results: list[BandResult]) -> str:
    """One per-cell markdown section: band comparison table + disjoint ranks."""
    lines: list[str] = [f"## Cell {cell_label}\n"]
    lines.append(
        "| (t1,t2) | median active | mean active | mean÷median | best-month | "
        "win-rate | avg_period | p14_share | active-DSR |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| ({r.t1},{r.t2}) | {_fmt(r.median_active)} | {_fmt(r.mean_active)} | "
            f"{_fmt(r.mean_over_median, '0.01')} | {_fmt(r.best_month_active)} | "
            f"{_fmt(r.win_rate, '0.01')} | {_fmt(r.avg_period, '0.01')} | "
            f"{_fmt(r.p14_share, '0.001')} | {_fmt(r.active_dsr, '0.0001')} |"
        )
    lines.append("")
    # disjoint-window rank table (G2): rank by median active in each half
    lines.append("### Disjoint-window rank (median active; lower rank = better)\n")
    early_rank = _rank_labels(results, key=lambda r: r.early_median_active)
    recent_rank = _rank_labels(results, key=lambda r: r.recent_median_active)
    lines.append("| (t1,t2) | rank pre-2022 | rank 2022+ |")
    lines.append("|---|---|---|")
    for r in results:
        label = f"({r.t1},{r.t2})"
        lines.append(f"| {label} | {early_rank[label]} | {recent_rank[label]} |")
    lines.append("")
    return "\n".join(lines)


def _rank_labels(results: list[BandResult], *, key) -> dict[str, int]:  # type: ignore[no-untyped-def]
    ordered = sorted(results, key=key, reverse=True)  # higher active = rank 1
    return {f"({r.t1},{r.t2})": i + 1 for i, r in enumerate(ordered)}


def render_report(sections: dict[str, list[BandResult]], *, data_window: str) -> str:
    """Full report: header + caveats + per-cell sections."""
    lines: list[str] = ["# Adaptive-Period Band (t1,t2) Sweep\n"]
    lines.append(f"**Data window**: {data_window}")
    lines.append("**Fill model**: linear, mean fill = 1.0 (the 100%-fill optimism caveat).")
    lines.append(
        "**Status**: characterization only — locked-but-not-armed, same as p_long=14. "
        "Not in cells.canary.yaml; zero live impact.\n"
    )
    lines.append("## Standing caveats\n")
    lines.append(
        "- 100%-fill optimism (mean fill = 1.0); EDA ratio_sigma from 2022-2026; "
        "tiers chosen-not-gated. The chosen band is a relative, backtest-internal pick, "
        "NOT a claim it reproduces live or resolves live≈0.\n"
        "- Robustness gate = disjoint pre-2022 vs 2022+ ranks (not nested). "
        "DSR is on the ACTIVE series (n_trials=8); the bot-vs-idle level DSR is non-gating.\n"
        "- Null result is valid: if survivors' paired difference CIs straddle 0, band is "
        "not a meaningful lever — keep the highest-t2 (least p14-tail) band.\n"
    )
    for cell_label, results in sections.items():
        lines.append(render_cell_section(cell_label, results))
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "render" -v`
Expected: PASS.

- [ ] **Step 5: Run full module suite + lint, then commit**

```bash
uv run pytest tests/modules/backtest/test_band_sweep.py -v
uv run mypy src/bfx_funding_bot/modules/backtest/band_sweep.py && uv run ruff check src/bfx_funding_bot/modules/backtest/band_sweep.py
git add -A && git commit -m "✨ Feat: band_sweep markdown render (per-cell table + disjoint ranks + caveats)"
```

---

### Task 8: async driver script

Fetches each cell's candles once (start at `--start-mts`, default 2016), enumerates the 8 bands borrowing `ratio_sigma` from `cells.experimental-p14.yaml`, evaluates each, builds `BandResult`s, writes `.md` + `.json`. Mirrors `run_oos_profitability.py` bootstrap (Settings → engine → session_scope per cell).

**Files:**
- Create: `scripts/run_adaptive_band_sweep.py`
- Test: `tests/modules/backtest/test_band_sweep.py` (a pure-helper test for the ratio_sigma loader; the async path is integration, run manually in Task 9)

- [ ] **Step 1: Write the failing test for the ratio_sigma loader (R5 guard)**

```python
# add to test_band_sweep.py
from pathlib import Path
from bfx_funding_bot.modules.backtest.band_sweep import load_cell_ratio_sigmas


def test_load_cell_ratio_sigmas_matches_p14_config() -> None:
    sigmas = load_cell_ratio_sigmas(Path("configs/cells.experimental-p14.yaml"))
    # 4 cells, sigma is strategy-independent EDA — the single source of truth (R5)
    assert set(sigmas) == {"fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2"}
    assert sigmas["fUST_a30"] == Decimal("0.42049266874194213")
    assert sigmas["fUSD_p2"] == Decimal("0.3450137927640065")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "ratio_sigmas" -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the loader in band_sweep.py**

```python
# add to band_sweep.py
from pathlib import Path

from bfx_funding_bot.modules.marketfeed.config import load_cells_only


def load_cell_ratio_sigmas(p14_yaml: Path) -> dict[str, Decimal]:
    """Per-cell ratio_sigma from the p14 experimental config — the single
    source of truth (strategy-independent EDA). Keyed by cell_id."""
    return {
        cell.cell_id: Decimal(str(cell.params["ratio_sigma"]))
        for cell in load_cells_only(p14_yaml)
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/modules/backtest/test_band_sweep.py -k "ratio_sigmas" -v`
Expected: PASS.

- [ ] **Step 5: Write the driver script** (no unit test — integration, exercised in Task 9)

```python
# scripts/run_adaptive_band_sweep.py
"""Adaptive-period band (t1,t2) sweep driver.

Evaluates the 8 (t1,t2) bands × 4 cells over a single full-history candle
fetch, splitting outcomes into disjoint pre-2022 / 2022+ halves post-hoc.
Writes a research report. Characterization only (locked-but-not-armed).

Spec:  docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md
Plan:  docs/superpowers/plans/2026-06-04-adaptive-period-band-sweep.md

Usage:
    cd backend_py
    uv run python scripts/run_adaptive_band_sweep.py \\
        --output ../docs/research/2026-06-04-adaptive-period-band-sweep.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.band_sweep import (
    BandResult,
    build_band_result,
    enumerate_bands,
    load_cell_ratio_sigmas,
    pairwise_tie_matrix,
    render_report,
    simulate_period_path,
)
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("run_adaptive_band_sweep")

P14_CONFIG = Path("configs/cells.experimental-p14.yaml")
DEFAULT_START_MTS = int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000)
P_MID, P_LONG, EMA_SPAN, N_TRIALS = 7, 14, 24, 8
CELLS = [("fUST", "a30"), ("fUST", "p2"), ("fUSD", "a30"), ("fUSD", "p2")]


async def _run_cell(
    session: AsyncSession, symbol: str, period_agg: str,
    ratio_sigma: Decimal, start_mts: int,
) -> tuple[list[BandResult], list[dict[str, object]]]:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=symbol, timeframe="1h",
        period_agg=period_agg, start_mts=start_mts, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"No candles for {symbol}_{period_agg}; run backfill_candles.py")
    windows = compute_wfo_windows(candles)
    results: list[BandResult] = []
    labeled: list[tuple[tuple[Decimal, Decimal], list[WindowOutcome]]] = []
    for t1, t2 in enumerate_bands():
        params = dict(ema_span=EMA_SPAN, ratio_sigma=ratio_sigma, t1=t1, t2=t2,
                      p_mid=P_MID, p_long=P_LONG)

        def _make(p: dict[str, object] = params) -> Strategy:  # default-bind per iteration
            return AdaptivePeriodStrategy(**p)  # type: ignore[arg-type]

        strat_outcomes, base_outcomes = evaluate_oos_windows(candles, windows, make_strategy=_make)
        periods = simulate_period_path(candles, **params)  # type: ignore[arg-type]
        results.append(build_band_result(
            t1=t1, t2=t2, strat_outcomes=strat_outcomes, base_outcomes=base_outcomes,
            periods=periods, p_long=P_LONG, n_trials=N_TRIALS,
        ))
        labeled.append(((t1, t2), strat_outcomes))
    logger.info("%s_%s: %d windows × 8 bands", symbol, period_agg, len(windows))
    return results, pairwise_tie_matrix(labeled)


def _to_json(
    sections: dict[str, list[BandResult]],
    matrices: dict[str, list[dict[str, object]]],
) -> dict:  # type: ignore[type-arg]
    return {
        cell: {
            "bands": [{k: str(v) for k, v in r.__dict__.items()} for r in results],
            "pairwise_tie_matrix": [
                {k: str(v) for k, v in row.items()} for row in matrices[cell]
            ],
        }
        for cell, results in sections.items()
    }


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown path (.json sibling auto)")
    parser.add_argument("--start-mts", type=int, default=DEFAULT_START_MTS,
                        help="candle fetch start (default 2016-01-01)")
    args = parser.parse_args()

    sigmas = load_cell_ratio_sigmas(P14_CONFIG)
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    sections: dict[str, list[BandResult]] = {}
    matrices: dict[str, list[dict[str, object]]] = {}
    try:
        for symbol, period_agg in CELLS:
            cell_id = f"{symbol}_{period_agg}"
            async with session_scope(session_factory) as session:
                results, matrix = await _run_cell(
                    session, symbol, period_agg, sigmas[cell_id], args.start_mts,
                )
                sections[cell_id] = results
                matrices[cell_id] = matrix
    except Exception:
        logger.exception("band sweep failed")
        return 2
    finally:
        await engine.dispose()

    data_window = f"{datetime.fromtimestamp(args.start_mts / 1000, UTC):%Y-%m} .. now (split at 2022-01)"
    out = Path(args.output)
    out.write_text(render_report(sections, data_window=data_window))
    out.with_suffix(".json").write_text(json.dumps(_to_json(sections, matrices), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Lint + commit**

```bash
uv run mypy src/bfx_funding_bot/modules/backtest/band_sweep.py scripts/run_adaptive_band_sweep.py
uv run ruff check src/bfx_funding_bot/modules/backtest/band_sweep.py scripts/run_adaptive_band_sweep.py
git add -A && git commit -m "✨ Feat: adaptive-period band sweep driver script"
```

---

### Task 9: run the sweep + make the decision

This task EXECUTES the sweep and applies the §7 decision procedure. Its output — the chosen `(t1,t2)` — feeds Task 10. This is not a code task; it runs the driver and records the decision in the report.

- [ ] **Step 1: Confirm candles cover full history** (fUST 2018→, fUSD 2016→)

Run:
```bash
uv run python -c "
import asyncio
from datetime import datetime, UTC
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
async def m():
    s=Settings(); e=make_engine(s); sf=make_session_factory(e)
    async with session_scope(sf) as ss:
        for sym,pa in [('fUST','a30'),('fUSD','a30')]:
            c=await get_candles_in_range(ss,symbol=sym,timeframe='1h',period_agg=pa,start_mts=int(datetime(2016,1,1,tzinfo=UTC).timestamp()*1000),end_mts=int(datetime.now(UTC).timestamp()*1000))
            print(sym,pa,len(c),'first',datetime.fromtimestamp(c[0].mts/1000,UTC).date() if c else None)
    await e.dispose()
asyncio.run(m())
"
```
Expected: each cell returns thousands of candles starting 2016-2018. If a cell is empty/short, run `scripts/backfill_candles.py` first (see CLAUDE.md).

- [ ] **Step 2: Run the sweep**

Run:
```bash
uv run python scripts/run_adaptive_band_sweep.py \
    --output ../docs/research/2026-06-04-adaptive-period-band-sweep.md
```
Expected: writes `.md` + `.json`; logs `<cell>: N windows × 8 bands` for all 4 cells. ~minutes (8 bands × 4 cells × full-history backtests).

- [ ] **Step 3: Apply the §7 decision procedure to the report tables**

Working through the report `.md` (per-cell tables + disjoint ranks) and `.json`
(per-cell `pairwise_tie_matrix`) per spec §7:
1. Drop bands not in the top half of `median active` in **both** disjoint-rank columns (pre-2022 + 2022+) for ≥3 of 4 cells.
2. Drop fat-tail-inflated survivors (high mean÷median / best-month outliers).
3. For the surviving bands, read their pairs in the `.json` `pairwise_tie_matrix` (`tied: true/false` per pair, already computed by the driver). If every surviving pair is `tied` (or cells split 2-2 with no consistent winner) → **null result**.
4. Resolve: null → highest `t2` survivor (least p14-share, per `p14_share` column); else → plateau-centre knee (stable median active along both t1 and t2 axes).

- [ ] **Step 4: Append the decision to the report**

Add a `## Decision` section to the `.md` stating: the chosen `(t1,t2)`, which step resolved it (4-null or 4-edge), the runner-up + paired-difference margin, and whether per-cell bands were needed (default: single band). Mirror the `p_long` sweep's decision prose. Record the value as `CHOSEN_T1`, `CHOSEN_T2` for Task 10.

- [ ] **Step 5: Commit the report**

```bash
git add ../docs/research/2026-06-04-adaptive-period-band-sweep.md ../docs/research/2026-06-04-adaptive-period-band-sweep.json
git commit -m "📝 Docs: adaptive-period band sweep results + decision (t1,t2 chosen)"
```

---

### Task 10: fold p_long=14 + chosen band back into param_grid_for_cell

Uses Task 9's `CHOSEN_T1`/`CHOSEN_T2`. Resolves the span-168 question per spec §8 lean (a): drop the span-168 row so the grid represents the deployed candidate (AP runs span-24 everywhere; the WFO grid path is unused for AP). **Replace the placeholder band values below with Task 9's decision before implementing.**

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py:104-125`
- Modify: `tests/modules/backtest/strategies/test_adaptive_period.py:201-218`

- [ ] **Step 1: UPDATE the existing test to the new expectations**

Replace `test_param_grid_for_cell_from_eda` (currently asserts len==4, two spans, two band pairs, p_long=30) with the folded-back shape. Using the chosen band `(CHOSEN_T1, CHOSEN_T2)` from Task 9 (shown here as `0.5, 1.5` — **substitute the real decision**):

```python
def test_param_grid_for_cell_from_eda() -> None:
    grid = AdaptivePeriodStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={
            "close_over_ema_sigma_24": Decimal("0.05"),
            "close_over_ema_sigma_168": Decimal("0.08"),
        },
    )
    # Folded back to the deployed candidate (2026-06-04 band sweep): span-24 only,
    # single chosen band, p_long=14. span-168 row dropped (unused for AP — runs
    # span-24 in every cell; WFO grid path unused). See spec §8.
    assert len(grid) == 1
    assert grid[0]["ema_span"] == 24
    assert (grid[0]["t1"], grid[0]["t2"]) == (Decimal("0.5"), Decimal("1.5"))  # CHOSEN_T1/T2
    assert grid[0]["p_mid"] == 7 and grid[0]["p_long"] == 14
    assert grid[0]["ratio_sigma"] == Decimal("0.05")  # span-24 EDA sigma
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py::test_param_grid_for_cell_from_eda -v`
Expected: FAIL (current grid has len 4, p_long 30).

- [ ] **Step 3: Implement the fold-back**

Replace `param_grid_for_cell` (`adaptive_period.py:104-125`) with (substitute the real `CHOSEN_T1/T2`):

```python
    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Deployed candidate from the 2026-06-04 band sweep + 2026-06-03 p_long
        sweep: ema_span=24, band (t1,t2)=(0.5,1.5), p_mid=7, p_long=14. span-168
        dropped — AP runs span-24 in every experimental/canary cell and the WFO
        grid path is unused for AP (it goes through cells-config), so the grid
        represents the deployed candidate, not an untested span (spec §8).
        ratio_sigma injected from EDA close/EMA sigma (strategy-independent)."""
        sigma_24 = eda.get("close_over_ema_sigma_24", Decimal("0.05"))
        return [
            {
                "ema_span": 24, "ratio_sigma": sigma_24,
                "t1": Decimal("0.5"), "t2": Decimal("1.5"),  # CHOSEN_T1/T2 from Task 9
                "p_mid": 7, "p_long": 14,
            }
        ]
```

- [ ] **Step 4: Run the updated test + the full adaptive_period suite**

Run: `uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -v`
Expected: PASS (the updated test + all others green). If any OTHER test references the old grid (search `param_grid_for_cell`, `p_long.*30`, `0.05.*0.08`), update it to match — note the change in the commit.

- [ ] **Step 5: Add a guard test (grid can't silently revert)**

```python
# add to test_adaptive_period.py, mirroring test_p14_config_present
def test_param_grid_is_deployed_candidate_not_v1() -> None:
    """Guard: the fold-back must not silently revert to the v1 (p_long=30,
    two-band, two-span) grid (band sweep 2026-06-04)."""
    grid = AdaptivePeriodStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="a30",
        eda={"close_over_ema_sigma_24": Decimal("0.42")},
    )
    assert all(p["p_long"] == 14 for p in grid)        # not 30
    assert all(p["ema_span"] == 24 for p in grid)      # span-168 dropped
    assert {(p["t1"], p["t2"]) for p in grid} == {(Decimal("0.5"), Decimal("1.5"))}  # CHOSEN
```

- [ ] **Step 6: Full suite + lint + commit**

```bash
uv run pytest -m "not integration"
uv run mypy src/ && uv run ruff check
git add -A
git commit -m "✨ Feat: fold p_long=14 + swept band into param_grid_for_cell (drop span-168)"
```

Expected: full suite green; no diff to `cells.canary.yaml`, `engine.py`, `oos_eval.py`, or any live path.

---

## Final Verification (before finishing the branch)

- [ ] `cd backend_py && uv run pytest -m "not integration"` fully green.
- [ ] `uv run mypy src/ && uv run ruff check` clean.
- [ ] `git diff main --stat` touches ONLY: `band_sweep.py`, `run_adaptive_band_sweep.py`, `test_band_sweep.py`, `adaptive_period.py`, `test_adaptive_period.py`, and the `docs/research/2026-06-04-*` report. **No diff** to `cells.canary.yaml`, `engine.py`, `oos_eval.py`, `divergence_reporter.py`, or `derive_cells`.
- [ ] Report has all G1-G5 deliverables: 8×4 tables with avg_period + p14-share (G1), disjoint rank tables (G2), active-series DSR (G3), explicit §7 decision incl. null branch + p14-share tie-break (G4), param_grid folded (G5).
- [ ] Use `superpowers:finishing-a-development-branch` to decide merge/PR.

---

## Spec Coverage Check (self-review)

- §2.1 G1 → Task 7 render (both axes) + Task 9 run. G2 disjoint → Task 5 + 6 + 7. G3 active DSR → Task 3 + 6. G4 decision/null → Task 9. G5 fold-back → Task 10.
- §4 two new stats → Task 3 (active DSR) + Task 4 (paired diff). §5 2-D / p14-share → Task 2 + 6 + 7. §6 defenses → Tasks 3,4,5 + Task 9 procedure. §7 procedure → Task 9. §8 fold-back + span-168 + UPDATE test → Task 10. §9 OQ2 start-mts → Task 8. §12 tests → every task's TDD.
- Non-goals honored: no WFO, no ema_span sweep, no engine/oos_eval change, no canary/derive_cells/divergence touch (Final Verification gate).
