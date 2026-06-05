# Signal EDA Funnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline EDA funnel that scores 4 candidate signals (FRR-trend, SpikeDetect, funding_amount, utilization) for predictive power against forward market-rate movement, with regime-robustness checks and FDR correction, emitting a GO/KILL verdict per signal.

**Architecture:** A pure-function module `signal_eda.py` (pandas/numpy: data alignment, signal transforms, Spearman IC, quintile-spread, block-bootstrap, BH-FDR, GO/KILL decision, render) plus a thin driver `run_signal_eda.py` that pulls candles + funding_stats from live Neon (read-only), aligns them, runs the funnel across cell × horizon × regime, and writes a markdown + json report. No engine / live-path / canary changes — mirrors the existing `band_sweep` + `run_oos_profitability` split.

**Tech Stack:** Python 3.13, pandas 3.0, numpy 2.4, SQLAlchemy 2.0 async, pytest. Spearman via pandas `.corr(method="spearman")`; bootstrap + FDR hand-rolled with fixed seed for reproducibility.

---

## Spec reference

`docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md`

## File Structure

- Create `src/bfx_funding_bot/modules/backtest/signal_eda.py` — all pure functions (no I/O).
- Create `tests/modules/backtest/test_signal_eda.py` — unit tests on synthetic data.
- Create `scripts/run_signal_eda.py` — driver: DB pull → align → funnel → report.
- Create `tests/scripts/test_run_signal_eda.py` — driver smoke test.

## Conventions (read before starting)

- Run all commands from `backend_py/`: `cd backend_py && uv run pytest ...`.
- Signals operate on a pandas frame produced by `build_signal_frame`; columns are
  `float` (NaN for missing), not Decimal — EDA is statistical, Decimal precision
  is irrelevant here and pandas/numpy need floats.
- Fixed seed `SEED = 20260605` everywhere randomness appears (matches band_sweep's
  fixed-seed convention `_PAIRED_DIFF_SEED`).
- Horizons in **days**: `HORIZONS = [2, 7, 14, 30]`. Forward windows are
  **time-based** (ms), not index-based, so they are robust to candle granularity.
- Regime split point `SPLIT_MTS = 1_640_995_200_000` (2022-01-01 UTC) — disjoint
  early (2016–21) vs late (2022+), matching the band_sweep disjoint-windows rule.

---

### Task 1: Data alignment + forward target + regime split

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/backtest/test_signal_eda.py
from decimal import Decimal

import numpy as np
import pandas as pd

from bfx_funding_bot.modules.backtest.signal_eda import (
    SPLIT_MTS,
    add_forward_rate_change,
    build_signal_frame,
    split_regime,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

_DAY = 86_400_000


def _candles(closes: list[str], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingCandle(symbol="fUST", timeframe="1D", period_agg="p2",
                      mts=start + i * step, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def _stats(rows: list[dict], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingStat(symbol="fUST", mts=start + i * step,
                    frr=Decimal(str(r["frr"])),
                    funding_amount=Decimal(str(r.get("amt", 1000))),
                    funding_amount_used=Decimal(str(r.get("used", 500))))
        for i, r in enumerate(rows)
    ]


def test_build_signal_frame_aligns_stats_onto_candle_grid() -> None:
    candles = _candles(["0.0001", "0.0002", "0.0003"])
    stats = _stats([{"frr": 1e-6}, {"frr": 2e-6}, {"frr": 3e-6}])
    df = build_signal_frame(candles, stats)
    assert list(df["close"]) == [0.0001, 0.0002, 0.0003]
    assert df["frr"].tolist() == [1e-6, 2e-6, 3e-6]
    assert df["funding_amount"].tolist() == [1000.0, 1000.0, 1000.0]


def test_build_signal_frame_asof_uses_latest_prior_stat() -> None:
    # stat at t0 only; candles at t0, t1 -> t1 should carry t0's frr (backward asof)
    candles = _candles(["0.0001", "0.0002"])
    stats = _stats([{"frr": 5e-6}])  # single stat at t0
    df = build_signal_frame(candles, stats)
    assert df["frr"].tolist() == [5e-6, 5e-6]


def test_add_forward_rate_change_is_time_based_mean_minus_spot() -> None:
    # closes 1,2,3,4,5 on a daily grid; H=2 -> mean(next 2 closes) - spot
    df = build_signal_frame(_candles(["1", "2", "3", "4", "5"]), _stats([{"frr": 1e-6}] * 5))
    out = add_forward_rate_change(df, [2])
    # row0: mean(2,3) - 1 = 1.5 ; row1: mean(3,4) - 2 = 1.5 ; ...
    assert out["fwd_d2"].iloc[0] == 1.5
    assert out["fwd_d2"].iloc[1] == 1.5
    # last row has no forward candles -> NaN
    assert np.isnan(out["fwd_d2"].iloc[-1])


def test_split_regime_is_disjoint() -> None:
    early = build_signal_frame(_candles(["1"], start=SPLIT_MTS - 2 * _DAY), _stats([{"frr": 1e-6}], start=SPLIT_MTS - 2 * _DAY))
    late = build_signal_frame(_candles(["1"], start=SPLIT_MTS + _DAY), _stats([{"frr": 1e-6}], start=SPLIT_MTS + _DAY))
    combined = pd.concat([early, late], ignore_index=True)
    e, l = split_regime(combined)
    assert (e["mts"] < SPLIT_MTS).all()
    assert (l["mts"] >= SPLIT_MTS).all()
    assert set(e["mts"]).isdisjoint(set(l["mts"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError: cannot import name 'build_signal_frame'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/bfx_funding_bot/modules/backtest/signal_eda.py
"""Offline signal-screening EDA funnel.

Pure functions only (no DB/I/O). Scores candidate signals for predictive power
against forward market-rate movement. See
docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

SEED = 20260605
HORIZONS = [2, 7, 14, 30]
SPLIT_MTS = 1_640_995_200_000  # 2022-01-01 UTC; disjoint regime boundary


def _f(d: object) -> float:
    return float(d) if d is not None else float("nan")  # type: ignore[arg-type]


def build_signal_frame(
    candles: list[FundingCandle], stats: list[FundingStat]
) -> pd.DataFrame:
    """Align funding_stats onto the candle time grid via backward as-of join.

    Returns a frame sorted by mts with columns: mts, close, frr, avg_period,
    funding_amount, funding_amount_used. Each candle row carries the most recent
    funding_stat at or before its mts (merge_asof backward)."""
    c = (
        pd.DataFrame([{"mts": x.mts, "close": _f(x.close)} for x in candles])
        .sort_values("mts")
        .reset_index(drop=True)
    )
    s = (
        pd.DataFrame(
            [
                {
                    "mts": x.mts,
                    "frr": _f(x.frr),
                    "avg_period": _f(x.avg_period),
                    "funding_amount": _f(x.funding_amount),
                    "funding_amount_used": _f(x.funding_amount_used),
                }
                for x in stats
            ]
        )
        .sort_values("mts")
        .reset_index(drop=True)
    )
    return pd.merge_asof(c, s, on="mts", direction="backward")


def add_forward_rate_change(df: pd.DataFrame, horizons_days: list[int]) -> pd.DataFrame:
    """For each horizon H, add column fwd_d{H} = mean(close in (t, t+H days]) - close_t.

    Time-based (ms) window, robust to candle spacing. Rows without any forward
    candle inside the window get NaN."""
    out = df.copy()
    mts = out["mts"].to_numpy()
    close = out["close"].to_numpy()
    for h in horizons_days:
        horizon_ms = h * 86_400_000
        fwd = np.full(len(out), np.nan)
        for i in range(len(out)):
            mask = (mts > mts[i]) & (mts <= mts[i] + horizon_ms)
            if mask.any():
                fwd[i] = close[mask].mean() - close[i]
        out[f"fwd_d{h}"] = fwd
    return out


def split_regime(df: pd.DataFrame, split_mts: int = SPLIT_MTS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Disjoint early (mts < split) / late (mts >= split) regimes."""
    early = df[df["mts"] < split_mts].reset_index(drop=True)
    late = df[df["mts"] >= split_mts].reset_index(drop=True)
    return early, late
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda data alignment + forward-rate-change target + regime split"
```

---

### Task 2: Signal transforms (rolling/relative only)

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import (
    SIGNALS,
    amount_pctile,
    frr_trend,
    spike_z,
    utilization_pctile,
)


def test_spike_z_is_zero_on_flat_then_positive_on_spike() -> None:
    frr = [1e-6] * 30 + [1e-5]  # flat then 10x spike
    df = build_signal_frame(_candles(["1"] * 31), _stats([{"frr": v} for v in frr]))
    z = spike_z(df, w=14)
    assert z.iloc[-1] > 3.0  # spike is many sigma above rolling baseline
    assert abs(z.iloc[20]) < 1e-9  # flat region -> z ~ 0


def test_frr_trend_sign_tracks_direction() -> None:
    rising = [float(i) * 1e-6 for i in range(1, 41)]
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": v} for v in rising]))
    t = frr_trend(df, k=3)
    assert t.dropna().iloc[-1] > 0  # monotone rising -> positive trend


def test_pctile_is_in_unit_interval_and_high_at_max() -> None:
    amt = list(range(1, 41))
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": 1e-6, "amt": a} for a in amt]))
    p = amount_pctile(df, w=20)
    assert (p.dropna() >= 0).all() and (p.dropna() <= 1).all()
    assert p.iloc[-1] == 1.0  # latest is the max of its window


def test_utilization_uses_ratio_not_absolute() -> None:
    rows = [{"frr": 1e-6, "amt": 1000, "used": u} for u in range(100, 140)]
    df = build_signal_frame(_candles(["1"] * 40), _stats(rows))
    u = utilization_pctile(df, w=20)
    assert u.iloc[-1] == 1.0  # rising utilization -> latest at top percentile


def test_signals_registry_has_four_relative_signals() -> None:
    # GUARD: every signal must be a rolling/relative transform, never raw frr/amount.
    assert set(SIGNALS) == {"frr_trend", "spike_detect", "funding_amount", "utilization"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k "spike_z or frr_trend or pctile or utilization or registry" -v`
Expected: FAIL with `ImportError: cannot import name 'spike_z'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
from typing import Callable


def frr_trend(df: pd.DataFrame, k: int) -> pd.Series:
    """Normalized k-step FRR momentum: (frr_t - frr_{t-k}) / rolling_std. Relative,
    so per-year FRR slope drift does not contaminate it."""
    frr = df["frr"]
    delta = frr - frr.shift(k)
    return delta / frr.rolling(max(k * 4, 8), min_periods=k).std()


def spike_z(df: pd.DataFrame, w: int) -> pd.Series:
    """FRR z-score vs its own rolling baseline (spike detector). Relative form."""
    frr = df["frr"]
    return (frr - frr.rolling(w).mean()) / frr.rolling(w).std()


def _rolling_pctile(series: pd.Series, w: int) -> pd.Series:
    """Percentile rank of the latest value within its trailing window of size w."""
    return series.rolling(w).apply(lambda x: (x.iloc[-1] >= x).mean(), raw=False)


def amount_pctile(df: pd.DataFrame, w: int) -> pd.Series:
    """Rolling percentile rank of funding_amount (supply level, regime-free)."""
    return _rolling_pctile(df["funding_amount"], w)


def utilization_pctile(df: pd.DataFrame, w: int) -> pd.Series:
    """Rolling percentile of utilization = used/amount (demand pressure)."""
    util = df["funding_amount_used"] / df["funding_amount"]
    return _rolling_pctile(util, w)


# Signal registry: name -> (callable, default param). All rolling/relative.
SIGNALS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "frr_trend": lambda df: frr_trend(df, k=3),
    "spike_detect": lambda df: spike_z(df, w=14),
    "funding_amount": lambda df: amount_pctile(df, w=30),
    "utilization": lambda df: utilization_pctile(df, w=30),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda 4 rolling/relative signal transforms + registry guard"
```

---

### Task 3: Spearman IC + quintile-spread

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import quintile_spread, spearman_ic


def test_spearman_ic_recovers_planted_positive_correlation() -> None:
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(size=500))
    y = x + pd.Series(rng.normal(size=500)) * 0.3  # strong positive monotone link
    ic = spearman_ic(x, y)
    assert ic > 0.7


def test_spearman_ic_near_zero_on_independent_series() -> None:
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(size=500))
    y = pd.Series(rng.normal(size=500))
    assert abs(spearman_ic(x, y)) < 0.15


def test_spearman_ic_nan_when_too_few_pairs() -> None:
    assert np.isnan(spearman_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, np.nan])))


def test_quintile_spread_monotone_for_linear_link() -> None:
    x = pd.Series(np.linspace(0, 1, 200))
    y = x * 2.0
    spread, monotonic = quintile_spread(x, y)
    assert spread > 0
    assert monotonic is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k "spearman or quintile" -v`
Expected: FAIL with `ImportError: cannot import name 'spearman_ic'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
_MIN_IC_PAIRS = 10
_MIN_QUINTILE_PAIRS = 25


def spearman_ic(signal: pd.Series, target: pd.Series) -> float:
    """Spearman rank IC between signal_t and forward target. NaN if < 10 paired
    non-null observations."""
    pair = pd.concat([signal.reset_index(drop=True), target.reset_index(drop=True)], axis=1).dropna()
    if len(pair) < _MIN_IC_PAIRS:
        return float("nan")
    return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman"))


def quintile_spread(signal: pd.Series, target: pd.Series) -> tuple[float, bool]:
    """Mean target in the top signal-quintile minus the bottom, plus whether the
    5 bucket means are monotone. NaN spread if < 25 pairs or quintiles collapse."""
    pair = pd.concat(
        [signal.reset_index(drop=True).rename("s"), target.reset_index(drop=True).rename("t")],
        axis=1,
    ).dropna()
    if len(pair) < _MIN_QUINTILE_PAIRS:
        return float("nan"), False
    try:
        pair["q"] = pd.qcut(pair["s"], 5, labels=False, duplicates="drop")
    except ValueError:
        return float("nan"), False
    means = pair.groupby("q")["t"].mean()
    if len(means) < 2:
        return float("nan"), False
    spread = float(means.iloc[-1] - means.iloc[0])
    monotonic = bool(means.is_monotonic_increasing or means.is_monotonic_decreasing)
    return spread, monotonic
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda Spearman IC + quintile-spread"
```

---

### Task 4: Block-bootstrap CI + two-sided p-value

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import block_bootstrap_ic


def test_bootstrap_ci_excludes_zero_for_strong_link() -> None:
    rng = np.random.default_rng(2)
    x = pd.Series(rng.normal(size=400))
    y = x + pd.Series(rng.normal(size=400)) * 0.3
    res = block_bootstrap_ic(x, y)
    assert res.ci_lo > 0  # CI strictly above 0
    assert res.p_value < 0.05


def test_bootstrap_ci_includes_zero_for_noise() -> None:
    rng = np.random.default_rng(3)
    x = pd.Series(rng.normal(size=400))
    y = pd.Series(rng.normal(size=400))
    res = block_bootstrap_ic(x, y)
    assert res.ci_lo < 0 < res.ci_hi  # CI straddles 0
    assert res.p_value > 0.05


def test_bootstrap_is_reproducible_with_fixed_seed() -> None:
    rng = np.random.default_rng(4)
    x = pd.Series(rng.normal(size=200))
    y = x * 0.5 + pd.Series(rng.normal(size=200))
    a = block_bootstrap_ic(x, y)
    b = block_bootstrap_ic(x, y)
    assert a.ci_lo == b.ci_lo and a.ci_hi == b.ci_hi and a.p_value == b.p_value


def test_bootstrap_nan_when_too_few() -> None:
    res = block_bootstrap_ic(pd.Series([1.0] * 5), pd.Series([1.0] * 5))
    assert np.isnan(res.ci_lo)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k bootstrap -v`
Expected: FAIL with `ImportError: cannot import name 'block_bootstrap_ic'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
from dataclasses import dataclass

_MIN_BOOTSTRAP = 30


@dataclass(frozen=True)
class BootstrapIC:
    point: float       # IC on the full paired sample
    ci_lo: float
    ci_hi: float
    p_value: float     # two-sided: 2 * min(frac>0, frac<0) of bootstrap ICs
    n: int


def block_bootstrap_ic(
    signal: pd.Series,
    target: pd.Series,
    *,
    block_size: int = 20,
    n_boot: int = 1000,
    seed: int = SEED,
    alpha: float = 0.05,
) -> BootstrapIC:
    """Circular block-bootstrap CI + two-sided p-value for the Spearman IC.

    Block resampling preserves serial autocorrelation (plain bootstrap would
    understate the CI on a daily rate series). p_value is the two-sided bootstrap
    crossing rate. All NaN if < 30 paired observations."""
    pair = pd.concat(
        [signal.reset_index(drop=True), target.reset_index(drop=True)], axis=1
    ).dropna().to_numpy()
    n = len(pair)
    if n < _MIN_BOOTSTRAP:
        return BootstrapIC(float("nan"), float("nan"), float("nan"), float("nan"), n)
    point = float(pd.Series(pair[:, 0]).corr(pd.Series(pair[:, 1]), method="spearman"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_size))
    ics = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n, n_blocks)
        idx = np.concatenate([(np.arange(s, s + block_size) % n) for s in starts])[:n]
        samp = pair[idx]
        ics[b] = pd.Series(samp[:, 0]).rank().corr(pd.Series(samp[:, 1]).rank())
    lo, hi = np.nanpercentile(ics, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    frac_pos = float(np.mean(ics > 0))
    frac_neg = float(np.mean(ics < 0))
    p_value = 2.0 * min(frac_pos, frac_neg)
    return BootstrapIC(point, float(lo), float(hi), p_value, n)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda block-bootstrap IC CI + two-sided p-value"
```

---

### Task 5: Benjamini-Hochberg FDR

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import bh_fdr


def test_bh_fdr_rejects_clearly_small_p() -> None:
    rejected = bh_fdr([0.001, 0.002, 0.9, 0.8], alpha=0.05)
    assert rejected == [True, True, False, False]


def test_bh_fdr_marginal_p_passes_raw_but_fails_adjusted() -> None:
    # GUARD: a raw-p of 0.04 (passes un-adjusted 0.05) must be rejected by BH
    # when buried among many large p-values.
    pvals = [0.04] + [0.6] * 19  # 20 tests, only one smallish
    rejected = bh_fdr(pvals, alpha=0.05)
    assert rejected[0] is False  # BH threshold for rank 1 = (1/20)*0.05 = 0.0025
    assert not any(rejected)


def test_bh_fdr_empty() -> None:
    assert bh_fdr([], alpha=0.05) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k fdr -v`
Expected: FAIL with `ImportError: cannot import name 'bh_fdr'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
def bh_fdr(pvalues: list[float], *, alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg step-up. Returns a rejected-mask aligned to the input
    order. NaN p-values are treated as 1.0 (never rejected)."""
    m = len(pvalues)
    if m == 0:
        return []
    clean = [1.0 if (p is None or np.isnan(p)) else float(p) for p in pvalues]
    order = sorted(range(m), key=lambda i: clean[i])
    max_rank = 0
    for rank, i in enumerate(order, start=1):
        if clean[i] <= rank / m * alpha:
            max_rank = rank
    rejected = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= max_rank:
            rejected[i] = True
    return rejected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda Benjamini-Hochberg FDR correction"
```

---

### Task 6: GO/KILL decision over the cell × regime grid

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import CellRegimeIC, decide_signal


def _ic(cell, regime, ic, sig=True):
    return CellRegimeIC(cell=cell, regime=regime, horizon=7, ic=ic, fdr_significant=sig)


def test_go_when_robust_across_cells_and_regimes() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", 0.04),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", 0.04),
        _ic("fUSD_p2", "early", -0.01, sig=False), _ic("fUSD_p2", "late", 0.00, sig=False),
    ]
    v = decide_signal("frr_trend", obs)
    assert v.verdict == "GO"


def test_kill_when_sign_flips_across_regimes() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", -0.06),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", -0.05),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", -0.05),
        _ic("fUSD_p2", "early", 0.05), _ic("fUSD_p2", "late", -0.05),
    ]
    v = decide_signal("spike_detect", obs)
    assert v.verdict == "KILL"
    assert "regime" in v.reason.lower()


def test_kill_when_only_one_cell_significant() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.01, sig=False), _ic("fUST_p2", "late", 0.00, sig=False),
        _ic("fUSD_a30", "early", 0.00, sig=False), _ic("fUSD_a30", "late", 0.01, sig=False),
        _ic("fUSD_p2", "early", 0.00, sig=False), _ic("fUSD_p2", "late", 0.00, sig=False),
    ]
    v = decide_signal("utilization", obs)
    assert v.verdict == "KILL"


def test_kill_when_median_ic_below_floor() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.02), _ic("fUST_a30", "late", 0.02),
        _ic("fUST_p2", "early", 0.02), _ic("fUST_p2", "late", 0.02),
        _ic("fUSD_a30", "early", 0.02), _ic("fUSD_a30", "late", 0.02),
        _ic("fUSD_p2", "early", 0.02), _ic("fUSD_p2", "late", 0.02),
    ]
    v = decide_signal("funding_amount", obs)
    assert v.verdict == "KILL"
    assert "floor" in v.reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k "go_when or kill_when" -v`
Expected: FAIL with `ImportError: cannot import name 'decide_signal'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
IC_FLOOR = 0.03
CELL_MAJORITY = 3  # of 4 cells

_CELLS = ("fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2")


@dataclass(frozen=True)
class CellRegimeIC:
    cell: str
    regime: str  # "early" | "late"
    horizon: int
    ic: float
    fdr_significant: bool


@dataclass(frozen=True)
class SignalVerdict:
    signal: str
    verdict: str  # "GO" | "KILL"
    reason: str
    median_ic: float


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def decide_signal(signal: str, obs: list[CellRegimeIC]) -> SignalVerdict:
    """Apply the four GO gates (spec §2): FDR-significant, same sign across both
    regimes, same sign across >=3/4 cells, |median IC| >= floor. Otherwise KILL
    with the binding reason. Picks, per cell, the best-|IC| horizon's observations."""
    sig = [o for o in obs if o.fdr_significant]
    valid = [o.ic for o in obs if not np.isnan(o.ic)]
    median_ic = float(np.median(valid)) if valid else float("nan")

    if not sig:
        return SignalVerdict(signal, "KILL", "no FDR-significant IC in any cell/regime", median_ic)

    # cells where BOTH regimes are significant and agree in sign
    cells_robust: list[int] = []
    for cell in _CELLS:
        ce = [o for o in sig if o.cell == cell]
        early = [o for o in ce if o.regime == "early"]
        late = [o for o in ce if o.regime == "late"]
        if not early or not late:
            continue
        se = _sign(np.median([o.ic for o in early]))
        sl = _sign(np.median([o.ic for o in late]))
        if se != 0 and se == sl:
            cells_robust.append(se)

    if not cells_robust:
        return SignalVerdict(signal, "KILL", "sign flips across regimes (regime-fragile)", median_ic)
    if len(cells_robust) < CELL_MAJORITY:
        return SignalVerdict(
            signal, "KILL",
            f"only {len(cells_robust)}/4 cells robust (< {CELL_MAJORITY} majority)",
            median_ic,
        )
    # cells must agree in sign with each other (majority same sign)
    if abs(sum(_sign(s) for s in cells_robust)) < CELL_MAJORITY:
        return SignalVerdict(signal, "KILL", "robust cells disagree in sign", median_ic)
    if abs(median_ic) < IC_FLOOR:
        return SignalVerdict(
            signal, "KILL", f"median |IC| {abs(median_ic):.3f} below floor {IC_FLOOR}", median_ic
        )
    return SignalVerdict(signal, "GO", f"{len(cells_robust)}/4 cells robust, median IC {median_ic:.3f}", median_ic)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda GO/KILL decision over cell×regime grid (4 gates)"
```

---

### Task 7: Report renderer

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
- Test: `tests/modules/backtest/test_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/modules/backtest/test_signal_eda.py
from bfx_funding_bot.modules.backtest.signal_eda import SignalVerdict, render_report


def test_render_report_contains_verdict_and_table_headers() -> None:
    verdicts = [
        SignalVerdict("frr_trend", "GO", "3/4 cells robust, median IC 0.050", 0.05),
        SignalVerdict("utilization", "KILL", "no FDR-significant IC in any cell/regime", 0.00),
    ]
    md = render_report(verdicts, data_window="2016-2026, fUST 86 / fUSD 115 windows")
    assert "# Signal EDA Funnel" in md
    assert "frr_trend" in md and "GO" in md
    assert "utilization" in md and "KILL" in md
    assert "0.050" in md
    assert "2016-2026" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -k render -v`
Expected: FAIL with `ImportError: cannot import name 'render_report'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/bfx_funding_bot/modules/backtest/signal_eda.py
def render_report(verdicts: list[SignalVerdict], *, data_window: str) -> str:
    """Markdown summary: GO/KILL table + per-signal reason. Null-result safe
    (renders even if every signal is KILL)."""
    lines = [
        "# Signal EDA Funnel",
        "",
        f"**Data window:** {data_window}",
        "",
        "Screens 4 candidate signals (FRR-trend, SpikeDetect, funding_amount, "
        "utilization) for predictive power against forward rate change. GO requires "
        "FDR-significant IC, same sign across both regimes, >=3/4 cells robust, and "
        "|median IC| >= 0.03. Null result (all KILL) is a valid conclusion.",
        "",
        "## Verdicts",
        "",
        "| Signal | Verdict | Median IC | Reason |",
        "| --- | --- | --- | --- |",
    ]
    go = [v for v in verdicts if v.verdict == "GO"]
    for v in verdicts:
        lines.append(f"| {v.signal} | **{v.verdict}** | {v.median_ic:.3f} | {v.reason} |")
    lines += ["", "## Promotion", ""]
    if go:
        lines.append("Signals to promote to full strategy (engine plumbing + WFO/OOS):")
        lines += [f"- **{v.signal}** (median IC {v.median_ic:.3f})" for v in go]
    else:
        lines.append("**Null result** — no signal cleared the bar. None promoted. "
                     "Candidate pool unchanged; these signals are recorded as falsified.")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_signal_eda.py -v`
Expected: PASS (full module suite).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/signal_eda.py backend_py/tests/modules/backtest/test_signal_eda.py
git commit -m "✨ Feat: signal_eda report renderer (GO/KILL table, null-result safe)"
```

---

### Task 8: Driver script + smoke test

**Files:**
- Create: `scripts/run_signal_eda.py`
- Test: `tests/scripts/test_run_signal_eda.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_run_signal_eda.py
from decimal import Decimal

import numpy as np

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

from scripts.run_signal_eda import run_funnel_for_cell

_DAY = 86_400_000


def _series(n: int):
    rng = np.random.default_rng(0)
    base = 1_500_000_000_000
    frrs = rng.normal(2e-6, 5e-7, size=n).clip(min=1e-7)
    closes = (frrs * 1e2)  # close correlated to frr for a non-trivial IC
    candles = [
        FundingCandle(symbol="fUST", timeframe="1D", period_agg="p2",
                      mts=base + i * _DAY, open=Decimal(str(closes[i])), high=Decimal(str(closes[i])),
                      low=Decimal(str(closes[i])), close=Decimal(str(closes[i])))
        for i in range(n)
    ]
    stats = [
        FundingStat(symbol="fUST", mts=base + i * _DAY, frr=Decimal(str(frrs[i])),
                    funding_amount=Decimal("1000"), funding_amount_used=Decimal(str(400 + i % 50)))
        for i in range(n)
    ]
    return candles, stats


def test_run_funnel_for_cell_returns_one_obs_per_signal_regime() -> None:
    candles, stats = _series(300)
    obs = run_funnel_for_cell("fUST_p2", candles, stats)
    # 4 signals x 2 regimes x len(HORIZONS) observations, all CellRegimeIC
    signals = {o.signal_name for o in obs}
    assert signals == {"frr_trend", "spike_detect", "funding_amount", "utilization"}
    assert {o.regime for o in obs} == {"early", "late"} or {o.regime for o in obs} == {"late"}
    assert all(hasattr(o, "ic") for o in obs)


def test_run_funnel_handles_empty_gracefully() -> None:
    assert run_funnel_for_cell("fUST_p2", [], []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_signal_eda.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.run_signal_eda'`.

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/run_signal_eda.py
"""Signal EDA funnel driver.

Pulls candles + funding_stats from the configured DB (read-only SELECT), aligns
them, scores 4 candidate signals across cell x horizon x regime, applies BH-FDR,
and writes a GO/KILL markdown + json report.

Run:  cd backend_py && uv run python -m scripts.run_signal_eda --output \
      docs/research/2026-06-05-signal-eda-funnel.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.config import Settings
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.modules.backtest.config import load_cells_only
from bfx_funding_bot.modules.backtest.signal_eda import (
    HORIZONS,
    SIGNALS,
    BootstrapIC,
    CellRegimeIC,
    add_forward_rate_change,
    bh_fdr,
    block_bootstrap_ic,
    build_signal_frame,
    decide_signal,
    render_report,
    split_regime,
)
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.funding_stats.repository import get_in_range as get_stats_in_range

logger = logging.getLogger(__name__)
START_MTS = 1_451_606_400_000  # 2016-01-01 UTC (full history)
DEFAULT_CELLS_YAML = Path("configs/cells.canary.yaml")


@dataclass(frozen=True)
class _Obs:
    """One IC observation before FDR (carries signal name + p-value for BH)."""
    signal_name: str
    cell: str
    regime: str
    horizon: int
    ic: float
    p_value: float


def run_funnel_for_cell(cell_label: str, candles, stats) -> list[_Obs]:
    """Build the aligned frame, then compute one IC observation per
    signal x horizon x regime. Pure (no I/O) so it is unit-testable."""
    if not candles or not stats:
        return []
    frame = add_forward_rate_change(build_signal_frame(candles, stats), HORIZONS)
    early, late = split_regime(frame)
    out: list[_Obs] = []
    for regime_name, sub in (("early", early), ("late", late)):
        if len(sub) < 30:
            continue
        for sig_name, fn in SIGNALS.items():
            sig = fn(sub)
            for h in HORIZONS:
                res: BootstrapIC = block_bootstrap_ic(sig, sub[f"fwd_d{h}"])
                out.append(_Obs(sig_name, cell_label, regime_name, h, res.point, res.p_value))
    return out


def _apply_fdr_and_decide(all_obs: list[_Obs]) -> list:
    """BH-FDR across ALL observations, then reduce to per-signal verdicts."""
    pvals = [o.p_value for o in all_obs]
    rejected = bh_fdr(pvals)
    by_signal: dict[str, list[CellRegimeIC]] = {}
    for o, sig in zip(all_obs, rejected, strict=True):
        by_signal.setdefault(o.signal_name, []).append(
            CellRegimeIC(cell=o.cell, regime=o.regime, horizon=o.horizon,
                         ic=o.ic, fdr_significant=sig)
        )
    return [decide_signal(name, obs) for name, obs in sorted(by_signal.items())]


async def _run_cell(session: AsyncSession, cell, end_mts: int) -> list[_Obs]:
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    stats = await get_stats_in_range(session, symbol=cell.symbol, start_mts=START_MTS, end_mts=end_mts)
    return run_funnel_for_cell(cell.cell_id, candles, stats)


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown output path (.json sibling auto)")
    parser.add_argument("--cells", default=str(DEFAULT_CELLS_YAML))
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    all_obs: list[_Obs] = []
    try:
        for cell in load_cells_only(Path(args.cells)):
            async with session_scope(session_factory) as session:
                cell_obs = await _run_cell(session, cell, end_mts)
                logger.info("%s: %d observations", cell.cell_id, len(cell_obs))
                all_obs.extend(cell_obs)
    finally:
        await engine.dispose()

    verdicts = _apply_fdr_and_decide(all_obs)
    n_cells = len({o.cell for o in all_obs})
    md = render_report(verdicts, data_window=f"{START_MTS}–now, {n_cells} cells, {len(all_obs)} observations")
    out_path = Path(args.output)
    out_path.write_text(md)
    out_path.with_suffix(".json").write_text(json.dumps(
        [{"signal": v.signal, "verdict": v.verdict, "median_ic": v.median_ic, "reason": v.reason}
         for v in verdicts], indent=2))
    logger.info("wrote %s (+ .json)", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_signal_eda.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/scripts/run_signal_eda.py backend_py/tests/scripts/test_run_signal_eda.py
git commit -m "✨ Feat: run_signal_eda driver (DB pull → align → funnel → FDR → report)"
```

---

### Task 9: Full-suite gate + live run + report commit

**Files:**
- Create: `docs/research/2026-06-05-signal-eda-funnel.md` (+ `.json`, generated)

- [ ] **Step 1: Run the full unit suite + lint + types**

Run:
```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ scripts/ && uv run ruff check
```
Expected: all green (new tests included). Fix any failures before proceeding.

- [ ] **Step 2: Execute the live EDA run (read-only SELECT against Neon)**

Run:
```bash
cd backend_py && uv run python -m scripts.run_signal_eda \
  --output docs/research/2026-06-05-signal-eda-funnel.md
```
Expected: writes `docs/research/2026-06-05-signal-eda-funnel.md` + `.json`. Per-cell
observation counts logged. NOTE: `.env` points at live Neon; this is a read-only
SELECT — no writes, no migration, no engine/canary impact.

- [ ] **Step 3: Sanity-check the report**

Read `docs/research/2026-06-05-signal-eda-funnel.md`. Confirm: each of the 4 signals
has a verdict; median IC values are finite; a null result (all KILL) is reported
cleanly if no signal clears the bar. Do NOT manufacture a GO — null is valid.

- [ ] **Step 4: Commit the report**

```bash
git add backend_py/docs/research/2026-06-05-signal-eda-funnel.md backend_py/docs/research/2026-06-05-signal-eda-funnel.json
git commit -m "📝 Docs: signal EDA funnel results + GO/KILL verdicts"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Forward-return target `Δ_H` → Task 1 (`add_forward_rate_change`). ✓
- 4 rolling/relative signals (absolute forbidden) → Task 2 (+ registry guard test). ✓
- Spearman IC primary + quintile-spread secondary → Task 3. ✓
- Block-bootstrap significance → Task 4. ✓
- BH-FDR multiple-testing → Task 5 (+ marginal-p guard test). ✓
- GO/KILL four gates (FDR-sig + 2-regime same sign + ≥3/4 cells + |median IC|≥0.03) → Task 6. ✓
- Disjoint regime split → Task 1 (`split_regime` + disjoint test). ✓
- Output md+json report, null-result first-class → Task 7 + Task 9. ✓
- Driver read-only DB, no engine/live/canary → Task 8. ✓
- TDD synthetic-data tests throughout → every task. ✓

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `BootstrapIC.point` used as IC in Task 8; `CellRegimeIC` fields
(`cell/regime/horizon/ic/fdr_significant`) consistent across Tasks 6 & 8;
`SignalVerdict` fields (`signal/verdict/reason/median_ic`) consistent Tasks 6 & 7;
`SIGNALS` keys (`frr_trend/spike_detect/funding_amount/utilization`) consistent
Tasks 2, 6-test, 8. The driver's internal `_Obs.signal_name` maps to
`CellRegimeIC` (no name field) correctly in `_apply_fdr_and_decide`. ✓
