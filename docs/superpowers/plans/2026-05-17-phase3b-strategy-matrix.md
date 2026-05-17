# v2 Phase 3b — Strategy Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 3b: 3 candidate strategies × 6 data cells (2 symbols × 3 period_aggs) OOS matrix on post-2022 candles, with EDA-driven param grids, train/test 70/30 split, Sortino-based in-sample sweep, and consistency-based晋級 decision rule.

**Architecture:**
- Extend Strategy interface with stateful `observe()` method + classmethod `param_grid_for_cell()`
- Extend `run_backtest` engine with `record_start_mts` / `record_end_mts` warmup window + Sortino computation on month-end equity curve
- New helper modules: `backtest/split.py` (train/test split) and `backtest/sortino.py` (monthly sampling + ratio formula)
- 3 new strategies: `RatePercentileStrategy`, `MeanReversionStrategy`, `WeekendPremiumStrategy`
- New scripts: `eda_phase3b.py` (EDA on per-cell train portion) and `run_phase3b_matrix.py` (sweep + OOS + report)
- Output: `docs/research/2026-05-17-phase3b-eda.md` and `docs/research/2026-05-17-phase3b-results.md`

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Pydantic v2 (Decimal), pytest, uv. numpy/pandas already dev deps (Phase 3a).

**Spec:** `docs/superpowers/specs/2026-05-17-phase3b-strategy-matrix-design.md`

**Working directory:** All `pytest / mypy / ruff / alembic / uv run` commands must run from `backend_py/`. Scripts invoked via `cd backend_py && uv run python scripts/<name>.py`.

---

## Phase A — Foundations

### Task 1: `compute_train_end_mts` split helper

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/split.py`
- Test: `backend_py/tests/modules/backtest/test_split.py`

**Why:** Shared by EDA script and matrix runner; per spec must be single source of truth so train_end_mts is identical in both flows.

- [ ] **Step 1: Write the failing tests**

```python
# backend_py/tests/modules/backtest/test_split.py
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(n: int) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1704067200000 + i * 3_600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_compute_train_end_mts_70_30_split_on_10_candles_returns_7th_candle_mts() -> None:
    candles = _candles(10)
    # split_idx = int(10 * 0.7) = 7 → return candles[6].mts (zero-indexed: candle 0..6 = 7 candles in train)
    expected = candles[6].mts
    assert compute_train_end_mts(candles) == expected


def test_compute_train_end_mts_handles_unsorted_input() -> None:
    candles = _candles(10)
    shuffled = [candles[5], candles[0], candles[9], *candles[1:5], *candles[6:9]]
    assert compute_train_end_mts(shuffled) == candles[6].mts


def test_compute_train_end_mts_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_train_end_mts([])


def test_compute_train_end_mts_handles_single_candle() -> None:
    candles = _candles(1)
    # split_idx = int(1 * 0.7) = 0 → would access candles[-1]; should still return that candle's mts
    assert compute_train_end_mts(candles) == candles[0].mts


def test_compute_train_end_mts_handles_two_candles() -> None:
    candles = _candles(2)
    # split_idx = int(2 * 0.7) = 1 → return candles[0].mts
    assert compute_train_end_mts(candles) == candles[0].mts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_split.py -v`
Expected: ImportError / "module not found"

- [ ] **Step 3: Implement `split.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/split.py
"""Train/test split helper for Phase 3b sweep / eval flow.

Single source of truth for the 70/30 cut; consumed by both EDA script
and matrix runner so train_end_mts matches across the two flows.
"""
from __future__ import annotations

from bfx_funding_bot.modules.candles.schemas import FundingCandle

TRAIN_RATIO = 0.7


def compute_train_end_mts(candles: list[FundingCandle]) -> int:
    """Return the mts (inclusive) of the last candle in the train portion.

    Sorts candles by mts ascending then takes index `int(n * 0.7) - 1`,
    handling n=1 by returning the single candle's mts.
    """
    if not candles:
        raise ValueError("compute_train_end_mts: candles list is empty")
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    split_idx = int(len(sorted_candles) * TRAIN_RATIO)
    train_last_idx = max(0, split_idx - 1)
    return sorted_candles[train_last_idx].mts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_split.py -v`
Expected: 5 passed

- [ ] **Step 5: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/split.py \
        backend_py/tests/modules/backtest/test_split.py
git commit -m "$(cat <<'EOF'
✨ Feat: compute_train_end_mts split helper for Phase 3b

Single source of truth for 70/30 train/test cut, consumed by EDA
script + matrix runner so both flows compute identical train_end_mts.

Per Phase 3b spec section "Step 0 — Exploratory Data Analysis".

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Sortino computation module

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/sortino.py`
- Test: `backend_py/tests/modules/backtest/test_sortino.py`

**Why:** Sortino on month-end equity curve sampling is the in-sample sweep metric. Per spec sections "Sortino 計算" and "Risks" — must be independent helper so engine + tests can exercise the math without full backtest run.

- [ ] **Step 1: Write the failing tests**

```python
# backend_py/tests/modules/backtest/test_sortino.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.sortino import (
    compute_sortino,
    monthly_returns_from_equity_curve,
)


def test_monthly_returns_two_month_endpoints_returns_one_return() -> None:
    # equity_curve = [(mts1, equity1), (mts2, equity2)]
    # mts1 = 2024-01-31 23:00 UTC; mts2 = 2024-02-29 23:00 UTC
    curve = [
        (1706742000000, Decimal("1.00")),  # 2024-01-31 23:00 UTC
        (1709247600000, Decimal("1.01")),  # 2024-02-29 23:00 UTC
    ]
    rets = monthly_returns_from_equity_curve(curve)
    assert len(rets) == 1
    assert abs(rets[0] - Decimal("0.01")) < Decimal("0.0001")


def test_monthly_returns_empty_curve_returns_empty() -> None:
    assert monthly_returns_from_equity_curve([]) == []


def test_monthly_returns_single_point_returns_empty() -> None:
    curve = [(1706742000000, Decimal("1.00"))]
    assert monthly_returns_from_equity_curve(curve) == []


def test_compute_sortino_with_mixed_returns() -> None:
    # 3 positives + 1 negative: mean = (0.02 + 0.03 + 0.04 - 0.01)/4 = 0.02
    # downside std = std([-0.01]) = 0 → undefined, but per spec single-downside-obs std=0 → return +inf
    returns = [Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("-0.01")]
    sortino = compute_sortino(returns)
    # With only 1 downside obs, std=0 → +inf path
    assert sortino == Decimal("Infinity")


def test_compute_sortino_with_no_downside_returns_positive_infinity() -> None:
    returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
    sortino = compute_sortino(returns)
    assert sortino == Decimal("Infinity")


def test_compute_sortino_with_fewer_than_three_returns_returns_zero() -> None:
    assert compute_sortino([Decimal("0.01"), Decimal("0.02")]) == Decimal("0")
    assert compute_sortino([Decimal("0.01")]) == Decimal("0")
    assert compute_sortino([]) == Decimal("0")


def test_compute_sortino_with_multiple_downside_returns_finite_value() -> None:
    # returns: 0.05, 0.03, -0.02, -0.04
    # mean = 0.005
    # downside = [-0.02, -0.04]; mean_downside = -0.03; var = ((0.01)^2 + (0.01)^2)/2 = 1e-4; std = 0.01
    # sortino = 0.005 / 0.01 = 0.5
    returns = [Decimal("0.05"), Decimal("0.03"), Decimal("-0.02"), Decimal("-0.04")]
    sortino = compute_sortino(returns)
    assert abs(sortino - Decimal("0.5")) < Decimal("0.01")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_sortino.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `sortino.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/sortino.py
"""Sortino ratio on month-end equity curve sampling.

Per Phase 3b spec — Sortino is computed from monthly equity-curve
returns (sampled at month-end timestamps), not from per-trade returns.
This makes the metric independent of trade frequency and comparable
across strategies.

Edge cases:
- len(monthly_returns) < 3      → 0     (sample too small to trust)
- len(downside_returns) == 0    → +inf  (no downside observed; will be
                                          tie-broken by raw return in sweep)
- downside std == 0 with ≥1 obs → +inf  (degenerate, treated same as above)
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def monthly_returns_from_equity_curve(
    curve: list[tuple[int, Decimal]],
) -> list[Decimal]:
    """Compute month-over-month returns from an equity curve.

    Args:
        curve: list of (mts_ms, equity) tuples sampled at month-end timestamps,
               sorted ascending by mts.

    Returns:
        List of returns r_i = equity[i+1] / equity[i] - 1 for adjacent pairs.
        Empty list if fewer than 2 points.
    """
    if len(curve) < 2:
        return []
    sorted_curve = sorted(curve, key=lambda p: p[0])
    out: list[Decimal] = []
    for prev, cur in zip(sorted_curve, sorted_curve[1:], strict=False):
        if prev[1] == 0:
            out.append(Decimal("0"))
        else:
            out.append(cur[1] / prev[1] - Decimal("1"))
    return out


def compute_sortino(returns: list[Decimal]) -> Decimal:
    """Sortino ratio: mean(returns) / std(downside_returns).

    Per spec edge-case ladder:
    - len(returns) < 3          → 0       (untrustworthy sample)
    - no downside obs OR std==0 → +inf    (sweep tie-broken by raw return)
    """
    if len(returns) < 3:
        return Decimal("0")
    n = Decimal(len(returns))
    mean = sum(returns, Decimal("0")) / n
    downside = [r for r in returns if r < 0]
    if not downside:
        return Decimal("Infinity")
    dn = Decimal(len(downside))
    dmean = sum(downside, Decimal("0")) / dn
    # population variance (denominator = N), not sample (N-1); matches numpy.std default
    var = sum((r - dmean) * (r - dmean) for r in downside) / dn
    if var == 0:
        return Decimal("Infinity")
    # Decimal sqrt: use built-in via Decimal.sqrt() (>=Python 3.x ok)
    std = var.sqrt()
    return mean / std


def month_end_timestamps_within(
    start_mts: int, end_mts: int
) -> list[int]:
    """Return month-end UTC timestamps (mts in ms) within [start_mts, end_mts].

    Month-end = last second of last day of each month at 23:59:59 UTC.
    Used to sample equity curve from engine.
    """
    if start_mts > end_mts:
        return []
    out: list[int] = []
    start_dt = datetime.fromtimestamp(start_mts / 1000, UTC)
    # iterate month-by-month starting from start month
    year, month = start_dt.year, start_dt.month
    while True:
        # last second of (year, month)
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        first_of_next = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=UTC)
        last_second = int(first_of_next.timestamp() * 1000) - 1000  # subtract 1s
        if last_second > end_mts:
            break
        if last_second >= start_mts:
            out.append(last_second)
        year, month = next_year, next_month
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_sortino.py -v`
Expected: 7 passed

- [ ] **Step 5: Add a test for `month_end_timestamps_within`**

```python
# Append to test_sortino.py
from bfx_funding_bot.modules.backtest.sortino import month_end_timestamps_within


def test_month_end_timestamps_within_jan_to_march_2024() -> None:
    # 2024-01-15 00:00 UTC → 2024-03-15 00:00 UTC
    start = int(datetime(2024, 1, 15, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2024, 3, 15, tzinfo=UTC).timestamp() * 1000)
    ts = month_end_timestamps_within(start, end)
    assert len(ts) == 2  # Jan-end + Feb-end (March-end > end)
    # Jan 31, 23:59:59 UTC
    jan_end = int(datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    feb_end = int(datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    assert ts[0] == jan_end
    assert ts[1] == feb_end


def test_month_end_timestamps_within_empty_when_start_after_end() -> None:
    assert month_end_timestamps_within(2000, 1000) == []
```

Add the `from datetime import UTC, datetime` import at top of test file if not already present.

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_sortino.py -v`
Expected: 9 passed

- [ ] **Step 6: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/sortino.py \
        backend_py/tests/modules/backtest/test_sortino.py
git commit -m "$(cat <<'EOF'
✨ Feat: Sortino computation on month-end equity sampling

Phase 3b in-sample sweep metric. Three pure helpers:
- monthly_returns_from_equity_curve: adjacent-pair returns
- compute_sortino: mean / downside_std with spec edge-case ladder
  (n<3→0, no downside→+inf, std=0→+inf)
- month_end_timestamps_within: UTC month-end ms generator

Engine consumption (sample equity at these ts) follows in next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Extend Strategy interface (`observe` + `param_grid_for_cell`)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/base.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_base.py` (new)

**Why:** Phase 3b strategies need history (`observe()` for every candle, even cooldown ones). Param grid hook lets EDA inject per-cell variants. Default `observe` no-op keeps AlwaysFRR + future stateless strategies trivial.

- [ ] **Step 1: Write the failing tests**

```python
# backend_py/tests/modules/backtest/strategies/test_base.py
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class _StubStrategy(Strategy):
    @property
    def name(self) -> str:
        return "stub"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        return None


def _candle() -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )


def test_observe_default_is_noop() -> None:
    s = _StubStrategy()
    # Should not raise
    s.observe(_candle())


def test_param_grid_for_cell_default_raises() -> None:
    with pytest.raises(NotImplementedError):
        _StubStrategy.param_grid_for_cell("fUST", "p2", eda={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_base.py -v`
Expected: `AttributeError: 'Strategy' has no attribute 'observe'` (or similar)

- [ ] **Step 3: Update `base.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/strategies/base.py
from abc import ABC, abstractmethod
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class Strategy(ABC):
    """Abstract base for backtest strategies.

    Stateful contract (Phase 3b):
      - observe(candle): called on every candle including cooldown bars.
        Update internal state here (indicators, rolling windows, EMAs).
        Default is no-op for stateless strategies.
      - decide(candle): called only on non-cooldown candles inside the
        engine's record window. Return a LendDecision or None.

    Param sweep (Phase 3b):
      - param_grid_for_cell(symbol, period_agg, eda): classmethod returning
        list of kwarg dicts to construct strategy variants for a given cell.
        Defaults to NotImplementedError; subclasses participating in the sweep
        must override.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    def observe(self, candle: FundingCandle) -> None:
        """Update internal state from candle. Default no-op."""

    @abstractmethod
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return list of kwarg dicts to instantiate variants for a cell.

        Subclasses override to participate in the Phase 3b sweep. The `eda`
        dict carries per-cell EDA outputs (sigmas, percentiles, effect sizes).
        """
        raise NotImplementedError(
            f"{cls.__name__} does not implement param_grid_for_cell"
        )
```

- [ ] **Step 4: Run tests + regression check**

Run: `cd backend_py && uv run pytest tests/modules/backtest/ -v`
Expected: all tests including new + existing AlwaysFRR tests pass.

- [ ] **Step 5: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/base.py \
        backend_py/tests/modules/backtest/strategies/test_base.py
git commit -m "$(cat <<'EOF'
✨ Feat: Strategy.observe() + param_grid_for_cell() hooks

Phase 3b stateful strategy contract. observe() defaults to no-op so
existing stateless strategies (AlwaysFRR) are unaffected.
param_grid_for_cell() classmethod lets each strategy expose its
EDA-driven variant list to the matrix runner.

Engine wiring (calling observe on every candle) follows next.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Engine record window + observe routing + Sortino in BacktestResult

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`
- Modify: `backend_py/tests/modules/backtest/test_engine.py` (add new tests)
- Modify: `backend_py/tests/modules/backtest/test_schemas.py` (add field test)

**Why:** Engine becomes the consumer of all three foundation modules — calls `strategy.observe()` per candle, restricts trades to `[record_start_mts, record_end_mts]`, samples equity at month-ends, populates `BacktestResult.sortino`.

- [ ] **Step 1: Write failing schema test**

Append to `tests/modules/backtest/test_schemas.py`:

```python
def test_backtest_result_has_sortino_field() -> None:
    r = BacktestResult(
        strategy_name="x", symbol="fUST", start_mts=0, end_mts=1,
        n_candles=0,
        gross_monthly_return_pct=Decimal("0"),
        net_monthly_return_pct=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        n_trades=0, fill_rate=Decimal("0"),
        sortino=Decimal("1.5"),
    )
    assert r.sortino == Decimal("1.5")
```

- [ ] **Step 2: Add `sortino` field to `BacktestResult`**

```python
# In backend_py/src/bfx_funding_bot/modules/backtest/schemas.py
class BacktestResult(BaseModel):
    # ... existing fields ...
    fill_rate: Decimal
    sortino: Decimal = Decimal("0")  # new: monthly-equity-sampled Sortino; 0 = untrustworthy
```

- [ ] **Step 3: Write failing engine tests for record window**

Append to `tests/modules/backtest/test_engine.py`:

```python
def test_run_backtest_observe_called_for_every_candle_including_cooldown() -> None:
    """observe() must be called on every candle, even during cooldown
    (so stateful indicators stay fresh)."""
    candles = _candles_constant_rate("0.0001", n=72)
    observations: list[int] = []

    class ObservingStrategy(Strategy):
        @property
        def name(self) -> str:
            return "obs"

        def observe(self, candle: FundingCandle) -> None:
            observations.append(candle.mts)

        def decide(self, candle: FundingCandle) -> LendDecision | None:
            if candle.close is None:
                return None
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)

    run_backtest(candles, ObservingStrategy())
    assert observations == [c.mts for c in candles]


def test_run_backtest_record_window_excludes_trades_outside() -> None:
    """Trades whose decision-candle.mts falls outside [record_start_mts,
    record_end_mts] must not be counted in n_trades or contribute to equity."""
    candles = _candles_constant_rate("0.0001", n=720)
    # Restrict recording to last 240 candles (~10 days)
    record_start_mts = candles[480].mts
    record_end_mts = candles[-1].mts

    full = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    windowed = run_backtest(
        candles, AlwaysFRRStrategy(period_days=2),
        record_start_mts=record_start_mts,
        record_end_mts=record_end_mts,
    )

    assert windowed.n_trades < full.n_trades
    assert windowed.n_trades > 0


def test_run_backtest_sortino_populated_on_long_series() -> None:
    """720+ hourly candles spans ~1 month. Single-month series has no
    monthly returns → sortino should stay at 0 (n<3 floor)."""
    candles = _candles_constant_rate("0.0001", n=720)
    result = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    # 30 days = single month, only 1 month-end → 0 returns → sortino=0
    assert result.sortino == Decimal("0")


def test_run_backtest_sortino_with_multi_month_series_positive_finite() -> None:
    """4 months of constant-positive returns → no downside → sortino=+inf."""
    candles = _candles_constant_rate("0.0001", n=24 * 30 * 4)  # ~4 months
    result = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    assert result.sortino == Decimal("Infinity")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_engine.py tests/modules/backtest/test_schemas.py -v`
Expected: new tests fail (signature mismatch / no sortino field).

- [ ] **Step 5: Update `engine.py`**

Replace `run_backtest` signature and main loop in `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`:

```python
import math
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision
from bfx_funding_bot.modules.backtest.sortino import (
    compute_sortino,
    month_end_timestamps_within,
    monthly_returns_from_equity_curve,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    if source == "candle_close":
        return candle.close
    raise ValueError(f"unsupported market_rate_source: {source!r}")


def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
) -> tuple[Decimal, Decimal]:
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        return decision.rate, Decimal("1")
    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    gross_rate = decision.rate * fill_prob
    return gross_rate, fill_prob


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,
    record_start_mts: int | None = None,
    record_end_mts: int | None = None,
) -> BacktestResult:
    """Run a strategy over a candle series and return summary metrics.

    Record window (Phase 3b):
      strategy.observe(candle) is called on every candle (incl. cooldown / outside window)
      so stateful indicators stay fresh during warmup. decide() is called only when:
        - candle index > cooldown_until_idx, AND
        - candle.mts in [record_start_mts, record_end_mts] (defaults: full span)
      Trades recorded only in this window contribute to n_trades / equity / sortino.
    """
    config = config or BacktestConfig()

    if not candles:
        return BacktestResult(
            strategy_name=strategy.name, symbol="",
            start_mts=0, end_mts=0, n_candles=0,
            gross_monthly_return_pct=Decimal("0"),
            net_monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0, fill_rate=Decimal("0"),
            sortino=Decimal("0"),
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    symbol = sorted_candles[0].symbol
    full_start_mts = sorted_candles[0].mts
    full_end_mts = sorted_candles[-1].mts

    effective_start = record_start_mts if record_start_mts is not None else full_start_mts
    effective_end = record_end_mts if record_end_mts is not None else full_end_mts

    gross_equity = Decimal("1")
    net_equity = Decimal("1")
    peak = net_equity
    max_dd = Decimal("0")
    n_trades = 0
    fill_prob_sum = Decimal("0")
    cooldown_until_idx = -1
    gap_candles = math.ceil(config.gap_minutes / 60)
    one_minus_fee = Decimal("1") - config.fee_rate

    # Equity curve: (mts, net_equity) sampled at every candle in record window;
    # we'll re-sample to month-end below.
    equity_timeline: list[tuple[int, Decimal]] = []

    for i, candle in enumerate(sorted_candles):
        strategy.observe(candle)

        if i <= cooldown_until_idx:
            continue
        if candle.mts < effective_start or candle.mts > effective_end:
            continue

        decision = strategy.decide(candle)
        if decision is None:
            continue

        gross_rate, fill_prob = _apply_friction(decision, candle, config)
        period = Decimal(decision.period_days)
        gross_equity = gross_equity * (Decimal("1") + gross_rate * period)
        net_rate = gross_rate * one_minus_fee
        net_equity = net_equity * (Decimal("1") + net_rate * period)

        n_trades += 1
        fill_prob_sum += fill_prob
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles

        if net_equity > peak:
            peak = net_equity
        dd = (peak - net_equity) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

        equity_timeline.append((candle.mts, net_equity))

    # Monthly return / time elapsed (use record window endpoints)
    if effective_end > effective_start:
        total_hours = Decimal(effective_end - effective_start) / Decimal(3_600_000)
    else:
        total_hours = Decimal("0")
    months_elapsed = total_hours / Decimal("720") if total_hours > 0 else Decimal("0")
    if months_elapsed > 0:
        gross_monthly = (gross_equity - Decimal("1")) / months_elapsed * Decimal("100")
        net_monthly = (net_equity - Decimal("1")) / months_elapsed * Decimal("100")
    else:
        gross_monthly = Decimal("0")
        net_monthly = Decimal("0")

    fill_rate = (fill_prob_sum / Decimal(n_trades)) if n_trades > 0 else Decimal("0")

    # Sortino on month-end equity sampling
    sortino_value = _compute_sortino_from_timeline(
        equity_timeline, effective_start, effective_end
    )

    return BacktestResult(
        strategy_name=strategy.name, symbol=symbol,
        start_mts=effective_start, end_mts=effective_end,
        n_candles=len(sorted_candles),
        gross_monthly_return_pct=gross_monthly,
        net_monthly_return_pct=net_monthly,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades, fill_rate=fill_rate,
        sortino=sortino_value,
    )


def _compute_sortino_from_timeline(
    equity_timeline: list[tuple[int, Decimal]],
    window_start_mts: int,
    window_end_mts: int,
) -> Decimal:
    """Sample net_equity at each month-end within the window, then compute
    Sortino on month-over-month returns.

    For month-end ts, take the most recent (mts, equity) point at or before
    that ts. If no trade has happened by month-end, equity = 1.0 (initial).
    """
    month_ends = month_end_timestamps_within(window_start_mts, window_end_mts)
    if not month_ends:
        return Decimal("0")
    samples: list[tuple[int, Decimal]] = []
    cursor = 0
    last_equity = Decimal("1")
    sorted_timeline = sorted(equity_timeline, key=lambda p: p[0])
    for me in month_ends:
        while cursor < len(sorted_timeline) and sorted_timeline[cursor][0] <= me:
            last_equity = sorted_timeline[cursor][1]
            cursor += 1
        samples.append((me, last_equity))
    rets = monthly_returns_from_equity_curve(samples)
    return compute_sortino(rets)
```

- [ ] **Step 6: Update `BacktestResult` start/end semantics in result helper if needed**

The `start_mts` and `end_mts` in the result now reflect the **record window**, not full candle span. Verify existing tests don't depend on full-span values (`test_run_backtest_constant_rate_produces_expected_monthly_return` uses no record window so behavior preserved).

- [ ] **Step 7: Run all backtest tests**

Run: `cd backend_py && uv run pytest tests/modules/backtest/ -v`
Expected: all tests pass (incl. existing + new).

- [ ] **Step 8: Run mypy + ruff + full test suite (non-integration)**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check && uv run pytest -m "not integration" -q`
Expected: all clean.

- [ ] **Step 9: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/engine.py \
        backend_py/src/bfx_funding_bot/modules/backtest/schemas.py \
        backend_py/tests/modules/backtest/test_engine.py \
        backend_py/tests/modules/backtest/test_schemas.py
git commit -m "$(cat <<'EOF'
✨ Feat: engine record window + observe routing + Sortino metric

run_backtest gains record_start_mts / record_end_mts params: strategy.observe()
is called on every candle (warmup state stays fresh), but trades are only
recorded when candle.mts lies in the window. BacktestResult gains sortino,
computed by month-end-sampling net_equity then running mean/downside_std.

Phase 3b sweep + eval flow now end-to-end on the engine side.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase B — Exploratory Data Analysis

### Task 5: EDA script

**Files:**
- Create: `backend_py/scripts/eda_phase3b.py`
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/eda.py` (pure functions; testable)
- Test: `backend_py/tests/modules/backtest/test_eda.py`

**Why:** EDA outputs feed strategy param grids and drop rules. Pure-function module is unit-testable; script wraps it with Neon loading + output formatting.

- [ ] **Step 1: Write failing tests for EDA pure functions**

```python
# backend_py/tests/modules/backtest/test_eda.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.eda import (
    close_rate_percentiles,
    mean_rate_by_weekday,
    close_over_ema_sigma,
    autocorrelation_at_lags,
    per_quarter_regime_drift,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_close_rate_percentiles_returns_p25_p50_p75_p90() -> None:
    candles = [_candle(i, str(0.0001 * (i + 1))) for i in range(100)]
    pct = close_rate_percentiles(candles)
    assert "P25" in pct and "P50" in pct and "P75" in pct and "P90" in pct
    # values ascending
    assert pct["P25"] < pct["P50"] < pct["P75"] < pct["P90"]


def test_mean_rate_by_weekday_returns_seven_entries() -> None:
    # 14 hourly candles starting Mon 2024-01-01 00:00 UTC
    start = 1704067200000
    candles = [_candle(start + i * 3_600_000, "0.0001") for i in range(14 * 24)]
    by_wd = mean_rate_by_weekday(candles)
    assert set(by_wd.keys()) == {0, 1, 2, 3, 4, 5, 6}
    # all means equal (constant rate)
    means = list(by_wd.values())
    assert all(abs(m - means[0]) < Decimal("0.0001") for m in means)


def test_close_over_ema_sigma_with_constant_returns_zero() -> None:
    candles = [_candle(i * 3_600_000, "0.0001") for i in range(200)]
    sigma = close_over_ema_sigma(candles, ema_span=24)
    assert sigma < Decimal("0.001")  # essentially zero


def test_autocorrelation_at_lags_constant_series_returns_nan_or_one() -> None:
    candles = [_candle(i * 3_600_000, "0.0001") for i in range(200)]
    # constant series: ACF undefined (var=0); we return None for undefined
    acf = autocorrelation_at_lags(candles, lags=[1, 24])
    assert acf[1] is None or acf[1] == Decimal("1")


def test_per_quarter_regime_drift_returns_max_min_ratio() -> None:
    # 8 quarters with mean rates: 0.0001, 0.0001, 0.0001, 0.0001, 0.0002, 0.0002, 0.0002, 0.0002
    # max/min relative drift = (0.0002 - 0.0001) / 0.0001 = 1.0
    candles = []
    quarter_seconds = 90 * 24 * 3600
    for q in range(8):
        rate = "0.0001" if q < 4 else "0.0002"
        for h in range(24):
            ts = (q * quarter_seconds + h * 3600) * 1000
            candles.append(_candle(ts, rate))
    drift = per_quarter_regime_drift(candles)
    assert drift > Decimal("0.99")  # ≈ 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_eda.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `eda.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/eda.py
"""Pure-function EDA helpers for Phase 3b.

All functions take a candle list and return summary statistics.
Callers (eda_phase3b.py script) load candles per cell from Neon and
restrict to train portion before calling these.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np

from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _closes(candles: list[FundingCandle]) -> list[Decimal]:
    return [c.close for c in candles if c.close is not None]


def close_rate_percentiles(candles: list[FundingCandle]) -> dict[str, Decimal]:
    """Return {P25, P50, P75, P90} of close rates."""
    closes_f = [float(c) for c in _closes(candles)]
    if not closes_f:
        return {k: Decimal("0") for k in ("P25", "P50", "P75", "P90")}
    p = np.percentile(closes_f, [25, 50, 75, 90])
    return {
        "P25": Decimal(str(float(p[0]))),
        "P50": Decimal(str(float(p[1]))),
        "P75": Decimal(str(float(p[2]))),
        "P90": Decimal(str(float(p[3]))),
    }


def mean_rate_by_weekday(candles: list[FundingCandle]) -> dict[int, Decimal]:
    """Return {0..6: mean_close_rate}. 0 = Monday (datetime.weekday)."""
    by_wd: dict[int, list[Decimal]] = {i: [] for i in range(7)}
    for c in candles:
        if c.close is None:
            continue
        wd = datetime.fromtimestamp(c.mts / 1000, UTC).weekday()
        by_wd[wd].append(c.close)
    return {
        wd: (sum(vs, Decimal("0")) / Decimal(len(vs)) if vs else Decimal("0"))
        for wd, vs in by_wd.items()
    }


def close_over_ema_sigma(candles: list[FundingCandle], ema_span: int) -> Decimal:
    """Return σ of (close / EMA(span) - 1) sequence."""
    closes = _closes(candles)
    if len(closes) < 2:
        return Decimal("0")
    alpha = 2 / (ema_span + 1)
    ema = float(closes[0])
    ratios = []
    for c in closes:
        cf = float(c)
        ema = alpha * cf + (1 - alpha) * ema
        if ema > 0:
            ratios.append(cf / ema - 1)
    if len(ratios) < 2:
        return Decimal("0")
    return Decimal(str(float(np.std(ratios))))


def autocorrelation_at_lags(
    candles: list[FundingCandle], lags: list[int]
) -> dict[int, Decimal | None]:
    """ACF of close at given hourly lags. None when var=0 / sample too small."""
    closes = np.array([float(c) for c in _closes(candles)])
    out: dict[int, Decimal | None] = {}
    for lag in lags:
        if len(closes) <= lag + 1:
            out[lag] = None
            continue
        x = closes[:-lag]
        y = closes[lag:]
        var_x = np.var(x)
        var_y = np.var(y)
        if var_x == 0 or var_y == 0:
            out[lag] = None
        else:
            corr = float(np.corrcoef(x, y)[0, 1])
            out[lag] = Decimal(str(corr))
    return out


def per_quarter_regime_drift(candles: list[FundingCandle]) -> Decimal:
    """Returns (max_quarter_mean - min_quarter_mean) / min_quarter_mean.

    Quarters bucketed by candle UTC month (Jan-Mar=Q1 etc.).
    Spec gate: drift >= 0.30 → mandatory WFO required, Phase 3b conclusions
    invalidated.
    """
    buckets: dict[tuple[int, int], list[Decimal]] = {}
    for c in candles:
        if c.close is None:
            continue
        dt = datetime.fromtimestamp(c.mts / 1000, UTC)
        quarter = (dt.month - 1) // 3 + 1
        buckets.setdefault((dt.year, quarter), []).append(c.close)
    if len(buckets) < 2:
        return Decimal("0")
    means = [
        sum(vs, Decimal("0")) / Decimal(len(vs)) for vs in buckets.values() if vs
    ]
    if not means:
        return Decimal("0")
    lo = min(means)
    hi = max(means)
    if lo == 0:
        return Decimal("0")
    return (hi - lo) / lo
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_eda.py -v`
Expected: 5 passed.

- [ ] **Step 5: Write the EDA script `scripts/eda_phase3b.py`**

```python
"""Phase 3b EDA — per-cell statistics on train portion of post-2022 candles.

Outputs JSON-ish markdown summary to stdout for review/commit to
docs/research/2026-05-17-phase3b-eda.md.

Per Phase 3b spec section "Step 0":
  - Only train portion of each cell is used (avoids data snooping)
  - Outputs per cell: percentiles, weekday means, close/EMA σ, ACF, regime drift

Usage:
    cd backend_py
    uv run python scripts/eda_phase3b.py > /tmp/phase3b_eda.txt
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from itertools import product

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.eda import (
    autocorrelation_at_lags,
    close_over_ema_sigma,
    close_rate_percentiles,
    mean_rate_by_weekday,
    per_quarter_regime_drift,
)
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("eda_phase3b")

SYMBOLS = ["fUSD", "fUST"]
PERIOD_AGGS = ["p2", "p30", "a30"]
WINDOW_START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
WINDOW_END_MTS = int(datetime.now(UTC).timestamp() * 1000)


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    print("# Phase 3b EDA — per-cell train-portion stats\n")
    print(f"Window: {datetime.fromtimestamp(WINDOW_START_MTS/1000, UTC).date()} → today UTC\n")

    try:
        for symbol, period_agg in product(SYMBOLS, PERIOD_AGGS):
            print(f"\n## Cell: {symbol} × {period_agg}\n")
            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session, symbol=symbol, timeframe="1h",
                    period_agg=period_agg,
                    start_mts=WINDOW_START_MTS, end_mts=WINDOW_END_MTS,
                )
            if len(candles) < 720:
                print(f"- SKIP: len(candles)={len(candles)} < 720 (1 month)")
                continue
            train_end_mts = compute_train_end_mts(candles)
            train = [c for c in candles if c.mts <= train_end_mts]

            pct = close_rate_percentiles(train)
            wd = mean_rate_by_weekday(train)
            sigma24 = close_over_ema_sigma(train, ema_span=24)
            sigma168 = close_over_ema_sigma(train, ema_span=168)
            acf = autocorrelation_at_lags(train, lags=[1, 24, 168, 720])
            drift = per_quarter_regime_drift(train)
            weekend_effect = (
                ((wd[4] + wd[5] + wd[6]) / 3) - ((wd[0] + wd[1] + wd[2] + wd[3]) / 4)
            ) / ((wd[0] + wd[1] + wd[2] + wd[3]) / 4) if wd[0] else None

            print(f"- n_candles (post-2022): {len(candles)}; train n: {len(train)}")
            print(f"- train_end_mts: {train_end_mts}")
            print(f"- Percentiles: P25={pct['P25']}, P50={pct['P50']}, P75={pct['P75']}, P90={pct['P90']}")
            print(f"- Mean rate by weekday (0=Mon..6=Sun): {dict(sorted(wd.items()))}")
            print(f"- Weekend (Fri/Sat/Sun) effect size vs weekday: {weekend_effect}")
            print(f"- close/EMA(24h) σ: {sigma24}")
            print(f"- close/EMA(168h) σ: {sigma168}")
            print(f"- ACF: {acf}")
            print(f"- Per-quarter regime drift: {drift}")
            if drift >= 0.30:
                print("- ⚠️  Drift ≥ 30% → spec gate: Phase 3b conclusions invalid; mandatory WFO upgrade required.")
            if weekend_effect is not None and weekend_effect < 0.05:
                print("- ℹ️  Weekend effect < 5%: WeekendPremium drop rule triggered for this cell.")
        return 0
    except Exception:
        logger.exception("EDA failed")
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run mypy + ruff on script**

Run: `cd backend_py && uv run mypy src/ scripts/eda_phase3b.py && uv run ruff check`
Expected: clean.

- [ ] **Step 7: Run full pytest suite (non-integration)**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/eda.py \
        backend_py/scripts/eda_phase3b.py \
        backend_py/tests/modules/backtest/test_eda.py
git commit -m "$(cat <<'EOF'
✨ Feat: Phase 3b EDA module + script

Pure-function eda.py exposes 5 stats (percentiles, weekday means,
close/EMA σ, ACF, per-quarter regime drift) used to set param grids
and trigger drop rules. eda_phase3b.py script iterates the 6 cells
on Neon, prints per-cell summary with WeekendPremium drop + regime
drift gates flagged inline.

Train portion only (uses compute_train_end_mts) — no test leakage.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Run EDA on Neon + write report + lock param grids

**Files:**
- Create: `docs/research/2026-05-17-phase3b-eda.md`

**Why:** Manual research task. Output of script becomes input to strategy `param_grid_for_cell` implementations.

- [ ] **Step 1: Run the EDA script against Neon**

Run: `cd backend_py && uv run python scripts/eda_phase3b.py > /tmp/phase3b_eda.txt && cat /tmp/phase3b_eda.txt`

Expected: 6 cell sections with stats. If `len(candles) < 720` for any cell, log it and proceed with remaining cells.

- [ ] **Step 2: Check the regime drift gate**

For each cell, verify `per-quarter regime drift < 0.30`. If any cell exceeds, **STOP** and report back:

> "Cell X had drift Y ≥ 0.30 — spec gate triggered: Phase 3b invalidated; mandatory WFO upgrade required. Halting plan execution."

If all cells under threshold, proceed.

- [ ] **Step 3: Check WeekendPremium drop rule per cell**

For each cell, note whether `weekend effect size < 0.05`. Cells where it's < 5% will be excluded from WeekendPremium sweep (matrix runner reads this).

- [ ] **Step 4: Write the EDA report**

Create `docs/research/2026-05-17-phase3b-eda.md` with:

```markdown
# Phase 3b — EDA Report (Train Portion)

**Date**: <date when script run>
**Window**: 2022-01-01 → <today UTC>
**Method**: per-cell train portion (post-2022, first 70% of mts-sorted candles)
**Spec**: docs/superpowers/specs/2026-05-17-phase3b-strategy-matrix-design.md

## Per-cell results

<paste eda_phase3b.py output, formatted nicely>

## Param Grid Lock-in

### RatePercentile

- `percentile`: 25, 50, 75 (canonical quartiles; not data-tuned)
- `lookback_hours`:
  - If max ACF at lag 168h across cells ≥ 0.3 → keep 168 and 720 variants
  - If < 0.3 → use 24 and 168 (drop 720)
  - **Decision**: <which based on EDA>

### MeanReversion (per-cell ratio_sigma injection)

| Cell | EMA(24h) σ | EMA(168h) σ |
|---|---|---|
| fUSD × p2 | <value> | <value> |
| ... | ... | ... |

Strategy receives `ratio_sigma` per cell from `eda` dict at param-grid construction time.

### WeekendPremium drop rule

| Cell | Weekend effect | Status |
|---|---|---|
| fUSD × p2 | <value> | <pass/drop> |
| ... | ... | ... |

## Phase 3b proceeds: yes / no

If yes — proceed to strategy implementations.
If no (regime drift gate failed in any cell) — escalate to WFO upgrade.
```

- [ ] **Step 5: Commit the EDA report**

```bash
git add docs/research/2026-05-17-phase3b-eda.md
git commit -m "$(cat <<'EOF'
📝 Docs: Phase 3b EDA results + param grid lock-in

Per-cell train-portion stats from scripts/eda_phase3b.py against Neon.
Records regime drift gate (per-quarter), WeekendPremium drop rule
trigger status per cell, and locks RatePercentile lookback +
MeanReversion ratio_sigma values to be consumed by matrix runner.

Spec gate: per-quarter drift < 0.30 verified → Phase 3b proceeds.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase C — Strategies

### Task 7: RatePercentileStrategy

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_rate_percentile.py`

- [ ] **Step 1: Write failing tests**

```python
# backend_py/tests/modules/backtest/strategies/test_rate_percentile.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_name_includes_params() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=168)
    assert s.name == "rate_percentile_p50_n168"


def test_decide_returns_none_during_warmup() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=10)
    for i in range(9):
        s.observe(_c(i, "0.0001"))
    # 9 observations, lookback 10 — still warming up
    d = s.decide(_c(9, "0.0001"))
    assert d is None


def test_decide_emits_when_close_above_threshold() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=4)
    for c in [_c(0, "0.0001"), _c(1, "0.0002"), _c(2, "0.0003"), _c(3, "0.0004")]:
        s.observe(c)
    # window now has [0.0001, 0.0002, 0.0003, 0.0004]; P50 = 0.00025
    cand = _c(4, "0.0005")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.rate == Decimal("0.0005")
    assert d.period_days == 2


def test_decide_returns_none_when_close_below_threshold() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=4)
    for c in [_c(0, "0.0001"), _c(1, "0.0002"), _c(2, "0.0003"), _c(3, "0.0004")]:
        s.observe(c)
    cand = _c(4, "0.0001")
    s.observe(cand)
    d = s.decide(cand)
    assert d is None


def test_decide_returns_none_when_close_is_none() -> None:
    s = RatePercentileStrategy(percentile=50, lookback_hours=2)
    s.observe(_c(0, "0.0001")); s.observe(_c(1, "0.0002"))
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=2,
        open=None, close=None, high=None, low=None, volume=None,
    )
    assert s.decide(cand) is None


def test_param_grid_for_cell_returns_six_variants_when_lookback_acf_ok() -> None:
    grid = RatePercentileStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"acf_168h_pass": True},
    )
    assert len(grid) == 6
    # all three percentiles × two lookbacks
    pcts = {p["percentile"] for p in grid}
    looks = {p["lookback_hours"] for p in grid}
    assert pcts == {25, 50, 75}
    assert looks == {168, 720}


def test_param_grid_for_cell_drops_720_lookback_when_acf_weak() -> None:
    """Per spec: ACF lag 168h < 0.3 → drop 720 variant (剩 168 only)."""
    grid = RatePercentileStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"acf_168h_pass": False},
    )
    assert len(grid) == 3  # 3 percentiles × 1 lookback
    looks = {p["lookback_hours"] for p in grid}
    assert looks == {168}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_rate_percentile.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `rate_percentile.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py
from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

import numpy as np

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class RatePercentileStrategy(Strategy):
    """Only lend when current close >= percentile of last N candles' closes."""

    def __init__(self, percentile: int, lookback_hours: int) -> None:
        self._percentile = percentile
        self._lookback_hours = lookback_hours
        self._window: deque[Decimal] = deque(maxlen=lookback_hours)

    @property
    def name(self) -> str:
        return f"rate_percentile_p{self._percentile}_n{self._lookback_hours}"

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is not None:
            self._window.append(candle.close)

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        if len(self._window) < self._lookback_hours:
            return None  # warmup
        threshold = Decimal(str(float(
            np.percentile([float(x) for x in self._window], self._percentile)
        )))
        if candle.close >= threshold:
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
        return None

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Lookback grid depends on ACF at lag 168h (per EDA result).
        Per spec section "Step 0 — EDA":
        - acf_168h_pass True (corr >= 0.3): {168, 720}
        - acf_168h_pass False:              {168} only (drop 720)
        """
        looks = [168, 720] if eda.get("acf_168h_pass", False) else [168]
        return [
            {"percentile": p, "lookback_hours": n}
            for p in (25, 50, 75) for n in looks
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_rate_percentile.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py \
        backend_py/tests/modules/backtest/strategies/test_rate_percentile.py
git commit -m "$(cat <<'EOF'
✨ Feat: RatePercentileStrategy (Phase 3b candidate 1)

Stateful: observe() appends close to deque(maxlen=lookback_hours);
decide() emits LendDecision only when close >= np.percentile(window, P).
Warmup: returns None until window is full.

param_grid_for_cell branches on EDA ACF(168h): pass→{168,720} else
{24,168}; percentile axis fixed {25,50,75}.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: MeanReversionStrategy

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_mean_reversion.py`

- [ ] **Step 1: Write failing tests**

```python
# backend_py/tests/modules/backtest/strategies/test_mean_reversion.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_name_includes_params() -> None:
    s = MeanReversionStrategy(ema_span=24, threshold_sigma=Decimal("1.0"),
                              ratio_sigma=Decimal("0.05"))
    assert s.name == "mean_reversion_ema24_sigma1.0"


def test_decide_returns_none_before_ema_initialized() -> None:
    s = MeanReversionStrategy(ema_span=24, threshold_sigma=Decimal("1.0"),
                              ratio_sigma=Decimal("0.05"))
    # No observe() called → EMA is None
    d = s.decide(_c(0, "0.0001"))
    assert d is None


def test_decide_emits_when_close_above_lower_band() -> None:
    s = MeanReversionStrategy(ema_span=2, threshold_sigma=Decimal("1.0"),
                              ratio_sigma=Decimal("0.05"))
    # warm EMA: with span=2, alpha = 2/3 ≈ 0.667
    s.observe(_c(0, "0.0001"))  # ema = 0.0001
    s.observe(_c(1, "0.0001"))  # ema ≈ 0.0001
    cand = _c(2, "0.0001")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.period_days == 2


def test_decide_returns_none_when_close_below_lower_band() -> None:
    s = MeanReversionStrategy(ema_span=2, threshold_sigma=Decimal("2.0"),
                              ratio_sigma=Decimal("0.10"))
    s.observe(_c(0, "0.0010"))
    s.observe(_c(1, "0.0010"))
    cand = _c(2, "0.0007")
    s.observe(cand)
    # ema ≈ 0.0009; deviation = (0.0007 - 0.0009) / 0.0009 ≈ -0.222
    # lower band = -2.0 * 0.10 = -0.20; deviation < -0.20 → None
    d = s.decide(cand)
    assert d is None


def test_decide_returns_none_when_close_is_none() -> None:
    s = MeanReversionStrategy(ema_span=24, threshold_sigma=Decimal("1.0"),
                              ratio_sigma=Decimal("0.05"))
    s.observe(_c(0, "0.0001"))
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(cand)  # no crash even with None close
    assert s.decide(cand) is None


def test_param_grid_for_cell_uses_eda_ratio_sigma() -> None:
    grid = MeanReversionStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"close_over_ema_sigma_24": Decimal("0.05"),
             "close_over_ema_sigma_168": Decimal("0.08")},
    )
    assert len(grid) == 6
    spans = {p["ema_span"] for p in grid}
    thresholds = {p["threshold_sigma"] for p in grid}
    assert spans == {24, 168}
    assert thresholds == {Decimal("0.5"), Decimal("1.0"), Decimal("1.5")}
    # each variant carries the EDA-injected ratio_sigma matching its ema_span
    for p in grid:
        if p["ema_span"] == 24:
            assert p["ratio_sigma"] == Decimal("0.05")
        else:
            assert p["ratio_sigma"] == Decimal("0.08")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_mean_reversion.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `mean_reversion.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py
from __future__ import annotations

from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


class MeanReversionStrategy(Strategy):
    """Pause lending when close has fallen too far below EMA.

    Lower band = -threshold_sigma * ratio_sigma. If
    (close - ema)/ema < lower_band, return None (wait for revert).
    Otherwise lend at close, period=2.
    """

    def __init__(
        self,
        ema_span: int,
        threshold_sigma: Decimal,
        ratio_sigma: Decimal,
    ) -> None:
        self._ema_span = ema_span
        self._threshold_sigma = threshold_sigma
        self._ratio_sigma = ratio_sigma
        self._alpha = Decimal(2) / Decimal(ema_span + 1)
        self._ema: Decimal | None = None

    @property
    def name(self) -> str:
        return f"mean_reversion_ema{self._ema_span}_sigma{self._threshold_sigma}"

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is None:
            return
        if self._ema is None:
            self._ema = candle.close
        else:
            self._ema = (
                self._alpha * candle.close + (Decimal("1") - self._alpha) * self._ema
            )

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None or self._ema is None or self._ema == 0:
            return None
        deviation = (candle.close - self._ema) / self._ema
        lower_band = -self._threshold_sigma * self._ratio_sigma
        if deviation < lower_band:
            return None
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Grid: ema_span ∈ {24, 168}, threshold_sigma ∈ {0.5, 1.0, 1.5}.
        ratio_sigma is injected per ema_span from EDA stats.
        """
        sigma_24 = eda.get("close_over_ema_sigma_24", Decimal("0.05"))
        sigma_168 = eda.get("close_over_ema_sigma_168", Decimal("0.05"))
        spans = [(24, sigma_24), (168, sigma_168)]
        return [
            {
                "ema_span": span,
                "threshold_sigma": ts,
                "ratio_sigma": sigma,
            }
            for span, sigma in spans
            for ts in (Decimal("0.5"), Decimal("1.0"), Decimal("1.5"))
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_mean_reversion.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run mypy + ruff**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py \
        backend_py/tests/modules/backtest/strategies/test_mean_reversion.py
git commit -m "$(cat <<'EOF'
✨ Feat: MeanReversionStrategy (Phase 3b candidate 2)

Stateful: observe() incrementally updates EMA; decide() blocks
emission when (close-ema)/ema falls below -threshold_sigma*ratio_sigma.
ratio_sigma is injected per ema_span from EDA close/EMA σ stats.

Param grid: ema_span ∈ {24, 168} × threshold_sigma ∈ {0.5, 1.0, 1.5}
= 6 variants per cell. ratio_sigma differs per ema_span.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: WeekendPremiumStrategy

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/weekend_premium.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_weekend_premium.py`

- [ ] **Step 1: Write failing tests**

```python
# backend_py/tests/modules/backtest/strategies/test_weekend_premium.py
from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.weekend_premium import (
    WeekendPremiumStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle_on(weekday: int, close: str = "0.0001") -> FundingCandle:
    """weekday: 0=Mon..6=Sun. Build a candle at midday UTC on that weekday."""
    # 2024-01-01 is a Monday (weekday=0); add weekday days
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    mts = int(base.timestamp() * 1000) + weekday * 86_400_000
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def test_name_includes_weekend_period() -> None:
    s = WeekendPremiumStrategy(weekend_period=14)
    assert s.name == "weekend_premium_14d"


def test_weekday_emits_period_2() -> None:
    s = WeekendPremiumStrategy(weekend_period=14)
    for wd in (0, 1, 2, 3):  # Mon-Thu
        cand = _candle_on(wd)
        d = s.decide(cand)
        assert d is not None
        assert d.period_days == 2


def test_friday_saturday_sunday_emit_weekend_period() -> None:
    s = WeekendPremiumStrategy(weekend_period=14)
    for wd in (4, 5, 6):  # Fri/Sat/Sun
        cand = _candle_on(wd)
        d = s.decide(cand)
        assert d is not None
        assert d.period_days == 14


def test_decide_returns_none_when_close_is_none() -> None:
    s = WeekendPremiumStrategy(weekend_period=14)
    cand = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1704067200000,
        open=None, close=None, high=None, low=None, volume=None,
    )
    assert s.decide(cand) is None


def test_param_grid_for_cell_returns_three_variants_when_eda_passes() -> None:
    grid = WeekendPremiumStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"weekend_effect_size": Decimal("0.08")},
    )
    assert len(grid) == 3
    periods = {p["weekend_period"] for p in grid}
    assert periods == {7, 14, 30}


def test_param_grid_for_cell_returns_empty_when_eda_below_threshold() -> None:
    grid = WeekendPremiumStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={"weekend_effect_size": Decimal("0.03")},
    )
    assert grid == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_weekend_premium.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `weekend_premium.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/strategies/weekend_premium.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

DROP_THRESHOLD = Decimal("0.05")


class WeekendPremiumStrategy(Strategy):
    """Lend longer (weekend_period days) when candle falls on Fri/Sat/Sun UTC,
    else default period=2.
    """

    def __init__(self, weekend_period: int) -> None:
        self._weekend_period = weekend_period

    @property
    def name(self) -> str:
        return f"weekend_premium_{self._weekend_period}d"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        weekday = datetime.fromtimestamp(candle.mts / 1000, UTC).weekday()
        period = self._weekend_period if weekday in (4, 5, 6) else 2
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=period)

    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Drop rule: if weekend effect size < 5% relative, this cell is excluded
        (return []). Otherwise return 3 variants on weekend_period.
        """
        effect = eda.get("weekend_effect_size", Decimal("0"))
        if effect is None or Decimal(str(effect)) < DROP_THRESHOLD:
            return []
        return [{"weekend_period": p} for p in (7, 14, 30)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_weekend_premium.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run mypy + ruff + full suite**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check && uv run pytest -m "not integration" -q`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/weekend_premium.py \
        backend_py/tests/modules/backtest/strategies/test_weekend_premium.py
git commit -m "$(cat <<'EOF'
✨ Feat: WeekendPremiumStrategy (Phase 3b candidate 3)

Stateless: emits LendDecision with period=weekend_period on Fri/Sat/Sun
UTC, else period=2. EDA-driven drop rule: param_grid_for_cell returns
empty when weekend effect size < 5% relative — that cell skips the
strategy entirely (consistency denominator adjusts accordingly).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase D — Matrix runner + results

### Task 10: Matrix runner + decision rule

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/matrix.py` (pure helpers; testable)
- Create: `backend_py/scripts/run_phase3b_matrix.py`
- Test: `backend_py/tests/modules/backtest/test_matrix.py`

**Why:** Pure module holds (a) sweep winner picker (Sortino + tie-break), (b) consistency decision-rule evaluator. Script wraps it with Neon loading + report writing.

- [ ] **Step 1: Write failing tests for matrix helpers**

```python
# backend_py/tests/modules/backtest/test_matrix.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.matrix import (
    pick_sweep_winner,
    evaluate_strategy_consistency,
)
from bfx_funding_bot.modules.backtest.schemas import BacktestResult


def _result(sortino: str, net: str, fill: str = "1.0", n_trades: int = 100) -> BacktestResult:
    return BacktestResult(
        strategy_name="x", symbol="fUST", start_mts=0, end_mts=1, n_candles=0,
        gross_monthly_return_pct=Decimal("0"),
        net_monthly_return_pct=Decimal(net),
        max_drawdown_pct=Decimal("0"),
        n_trades=n_trades, fill_rate=Decimal(fill),
        sortino=Decimal(sortino),
    )


def test_pick_sweep_winner_picks_highest_sortino_when_finite() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="0.5", net="0.3")),
        ({"p": 2}, _result(sortino="1.2", net="0.2")),
        ({"p": 3}, _result(sortino="0.9", net="0.4")),
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 2}


def test_pick_sweep_winner_ties_inf_breaks_on_raw_return() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="Infinity", net="0.3")),
        ({"p": 2}, _result(sortino="Infinity", net="0.5")),
        ({"p": 3}, _result(sortino="2.5", net="0.8")),  # ignored, +inf takes priority
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 2}  # highest net within +inf subset


def test_pick_sweep_winner_filters_health_gates() -> None:
    # fill_rate < 0.3 OR n_trades < 10 → excluded
    candidates = [
        ({"p": 1}, _result(sortino="5.0", net="1.0", fill="0.2", n_trades=100)),
        ({"p": 2}, _result(sortino="3.0", net="0.5", fill="0.5", n_trades=5)),
        ({"p": 3}, _result(sortino="1.0", net="0.3", fill="0.5", n_trades=20)),
    ]
    winner = pick_sweep_winner(candidates)
    assert winner is not None
    assert winner[0] == {"p": 3}


def test_pick_sweep_winner_returns_none_when_all_filtered() -> None:
    candidates = [
        ({"p": 1}, _result(sortino="5.0", net="1.0", fill="0.2", n_trades=100)),
        ({"p": 2}, _result(sortino="3.0", net="0.5", fill="0.5", n_trades=5)),
    ]
    assert pick_sweep_winner(candidates) is None


def test_evaluate_strategy_consistency_pass_when_4_of_6_beat_and_margin_gate_met() -> None:
    per_cell = [
        # (strategy_net, baseline_net) per cell, 6 cells
        (Decimal("0.40"), Decimal("0.30")),  # +33% margin
        (Decimal("0.31"), Decimal("0.30")),  # marginal beat
        (Decimal("0.35"), Decimal("0.30")),
        (Decimal("0.32"), Decimal("0.30")),
        (Decimal("0.25"), Decimal("0.30")),  # loses
        (Decimal("0.20"), Decimal("0.30")),  # loses
    ]
    verdict = evaluate_strategy_consistency(per_cell)
    assert verdict.qualifies is True
    assert verdict.cells_beating == 4
    assert verdict.max_margin > Decimal("0.30")


def test_evaluate_strategy_consistency_fail_when_no_cell_meets_margin() -> None:
    per_cell = [(Decimal("0.31"), Decimal("0.30")) for _ in range(6)]
    verdict = evaluate_strategy_consistency(per_cell)
    assert verdict.qualifies is False
    assert verdict.cells_beating == 6
    assert verdict.max_margin < Decimal("0.05")


def test_evaluate_strategy_consistency_adjusts_denominator_for_skipped_cells() -> None:
    # 4 cells played; 3 of 4 = 75% (>= 4/6 fraction 0.666...)
    per_cell = [
        (Decimal("0.40"), Decimal("0.30")),  # +33% margin
        (Decimal("0.35"), Decimal("0.30")),
        (Decimal("0.33"), Decimal("0.30")),
        (Decimal("0.25"), Decimal("0.30")),  # loses
    ]
    verdict = evaluate_strategy_consistency(per_cell, total_cells=6, skipped_cells=2)
    # 3/4 = 0.75 >= 4/6; passes consistency + margin
    assert verdict.qualifies is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_matrix.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `matrix.py`**

```python
# backend_py/src/bfx_funding_bot/modules/backtest/matrix.py
"""Phase 3b matrix helpers: sweep-winner selection + decision-rule evaluation.

Kept as pure functions so the matrix runner script is a thin
Neon-loading + report-writing wrapper.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import BacktestResult

FILL_FLOOR = Decimal("0.3")
MIN_TRADES = 10
MARGIN_THRESHOLD = Decimal("0.05")
CONSISTENCY_THRESHOLD = Decimal(4) / Decimal(6)  # 4/6 in eligible cells


def pick_sweep_winner(
    candidates: list[tuple[dict[str, Any], BacktestResult]],
) -> tuple[dict[str, Any], BacktestResult] | None:
    """Sweep tie-break:
    1. Drop candidates with fill_rate < 0.3 OR n_trades < 10.
    2. If any candidate has sortino == +Infinity, pick within the +inf
       subset by max net_monthly_return_pct.
    3. Otherwise pick by max sortino.
    Returns None when all candidates are filtered.
    """
    eligible = [
        (p, r) for p, r in candidates
        if r.fill_rate >= FILL_FLOOR and r.n_trades >= MIN_TRADES
    ]
    if not eligible:
        return None
    inf_subset = [(p, r) for p, r in eligible if r.sortino == Decimal("Infinity")]
    if inf_subset:
        return max(inf_subset, key=lambda pr: pr[1].net_monthly_return_pct)
    return max(eligible, key=lambda pr: pr[1].sortino)


@dataclass(frozen=True)
class StrategyVerdict:
    qualifies: bool
    cells_beating: int
    cells_played: int
    max_margin: Decimal


def evaluate_strategy_consistency(
    per_cell_pairs: list[tuple[Decimal, Decimal]],
    total_cells: int = 6,
    skipped_cells: int = 0,
) -> StrategyVerdict:
    """Per-cell list of (strategy_net, baseline_net). Skipped cells (EDA drop,
    no candidates) are excluded from the consistency denominator.

    qualifies = (cells_beating / cells_played) >= 4/6 AND max_margin > 0.05.
    """
    cells_played = total_cells - skipped_cells
    if cells_played <= 0:
        return StrategyVerdict(False, 0, 0, Decimal("0"))
    cells_beating = 0
    margins: list[Decimal] = []
    for strat_net, base_net in per_cell_pairs:
        if base_net <= 0:
            continue
        margin = (strat_net - base_net) / base_net
        if strat_net > base_net:
            cells_beating += 1
        margins.append(margin)
    max_margin = max(margins) if margins else Decimal("0")
    pass_consistency = Decimal(cells_beating) / Decimal(cells_played) >= CONSISTENCY_THRESHOLD
    pass_margin = max_margin > MARGIN_THRESHOLD
    return StrategyVerdict(
        qualifies=pass_consistency and pass_margin,
        cells_beating=cells_beating,
        cells_played=cells_played,
        max_margin=max_margin,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_matrix.py -v`
Expected: 7 passed.

- [ ] **Step 6: Write the matrix runner script `scripts/run_phase3b_matrix.py`**

```python
"""Phase 3b matrix runner — sweep + OOS eval over 3 strategies × 6 cells.

Loads candles per (symbol, period_agg) cell from Neon, splits 70/30 by mts,
sweeps each strategy's param grid on train portion, picks winner via
matrix.pick_sweep_winner, evaluates winner on test portion, writes a results
markdown report fragment to stdout. EDA inputs are passed via a JSON file
prepared from the EDA report.

Usage:
    cd backend_py
    uv run python scripts/run_phase3b_matrix.py \\
        --eda /path/to/eda.json \\
        > /tmp/phase3b_results.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.matrix import (
    evaluate_strategy_consistency, run_cell,
)
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.strategies.rate_percentile import RatePercentileStrategy
from bfx_funding_bot.modules.backtest.strategies.weekend_premium import WeekendPremiumStrategy
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("phase3b_matrix")

SYMBOLS = ["fUSD", "fUST"]
PERIOD_AGGS = ["p2", "p30", "a30"]
START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
STRATEGIES = [RatePercentileStrategy, MeanReversionStrategy, WeekendPremiumStrategy]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eda", required=True, type=Path,
                   help="Path to EDA JSON (per-cell dict of stats)")
    return p.parse_args()


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    eda_blob = json.loads(args.eda.read_text())  # {f"{sym}_{pa}": {stat: value}}

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    all_results: list[dict] = []
    baselines: dict[tuple[str, str], object] = {}

    try:
        for symbol, period_agg in product(SYMBOLS, PERIOD_AGGS):
            cell_key = f"{symbol}_{period_agg}"
            print(f"\n## Cell: {cell_key}")
            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session, symbol=symbol, timeframe="1h",
                    period_agg=period_agg,
                    start_mts=START_MTS, end_mts=end_mts,
                )
            if len(candles) < 720:
                print(f"- SKIP: len(candles)={len(candles)} < 720")
                continue
            train_end_mts = compute_train_end_mts(candles)
            test_start_mts = train_end_mts + 1
            eda_cell = {k: (Decimal(str(v)) if isinstance(v, (int, float, str)) else v)
                        for k, v in (eda_blob.get(cell_key) or {}).items()}

            # Baseline
            baseline = run_backtest(
                candles, AlwaysFRRStrategy(period_days=2),
                record_start_mts=test_start_mts,
            )
            baselines[(symbol, period_agg)] = baseline
            print(f"- Baseline (AlwaysFRR p=2): net={baseline.net_monthly_return_pct}%")

            # Strategy sweep + eval (delegated to matrix.run_cell — see Step 6)
            for strategy_class in STRATEGIES:
                outcome = run_cell(
                    strategy_class=strategy_class,
                    candles=candles,
                    eda_cell=eda_cell,
                    cell_key=cell_key,
                    baseline_net=baseline.net_monthly_return_pct,
                )
                if outcome.status == "ok":
                    print(f"- {strategy_class.__name__}: params={outcome.params} "
                          f"OOS net={outcome.oos_net}% "
                          f"fill={outcome.oos_fill_rate} sortino={outcome.oos_sortino}")
                else:
                    print(f"- {strategy_class.__name__}: {outcome.status}")
                all_results.append({
                    "strategy": outcome.strategy_name,
                    "cell": outcome.cell_key, "status": outcome.status,
                    "params": outcome.params,
                    "oos_net": str(outcome.oos_net) if outcome.oos_net is not None else None,
                    "oos_max_dd": str(outcome.oos_max_dd) if outcome.oos_max_dd is not None else None,
                    "oos_fill_rate": str(outcome.oos_fill_rate) if outcome.oos_fill_rate is not None else None,
                    "oos_sortino": str(outcome.oos_sortino) if outcome.oos_sortino is not None else None,
                    "baseline_net": str(outcome.baseline_net) if outcome.baseline_net is not None else None,
                })

        # Per-strategy consistency verdict
        print("\n## Decision rule verdicts\n")
        for strategy_class in STRATEGIES:
            pairs: list[tuple[Decimal, Decimal]] = []
            skipped = 0
            for r in all_results:
                if r["strategy"] != strategy_class.__name__:
                    continue
                if r["status"] != "ok":
                    skipped += 1
                    continue
                pairs.append((Decimal(r["oos_net"]), Decimal(r["baseline_net"])))
            verdict = evaluate_strategy_consistency(
                pairs, total_cells=6, skipped_cells=skipped,
            )
            print(f"- {strategy_class.__name__}: "
                  f"{verdict.cells_beating}/{verdict.cells_played} cells beat baseline, "
                  f"max_margin={verdict.max_margin:.3f}, qualifies={verdict.qualifies}")
        return 0
    except Exception:
        logger.exception("Matrix run failed")
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Add `run_cell` helper to `matrix.py` + synthetic-fixture integration test**

Per spec testing table: "Matrix runner end-to-end on a synthetic 100-candle fixture（不打 Neon）". Refactor the orchestration into a callable helper inside `matrix.py` so we can test it without Neon.

Add to `matrix.py`:

```python
from typing import Callable, Iterable

from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


@dataclass(frozen=True)
class CellOutcome:
    strategy_name: str
    cell_key: str
    status: str  # "ok" | "skipped:eda_drop" | "skipped:no_valid_candidate" | "errored"
    params: dict[str, Any] | None = None
    oos_net: Decimal | None = None
    oos_max_dd: Decimal | None = None
    oos_fill_rate: Decimal | None = None
    oos_sortino: Decimal | None = None
    baseline_net: Decimal | None = None


def run_cell(
    strategy_class: type[Strategy],
    candles: list[FundingCandle],
    eda_cell: dict[str, Any],
    cell_key: str,
    baseline_net: Decimal,
) -> CellOutcome:
    """Run sweep + OOS eval for one (strategy, cell) pair. Pure orchestration:
    callers supply already-loaded candles + EDA dict + baseline for cell.
    """
    try:
        grid = strategy_class.param_grid_for_cell(
            symbol=candles[0].symbol, period_agg=candles[0].period_agg, eda=eda_cell,
        )
        if not grid:
            return CellOutcome(strategy_class.__name__, cell_key, "skipped:eda_drop")
        train_end_mts = compute_train_end_mts(candles)
        test_start_mts = train_end_mts + 1
        candidates = []
        for params in grid:
            train_result = run_backtest(
                candles, strategy_class(**params),
                record_end_mts=train_end_mts,
            )
            candidates.append((params, train_result))
        winner = pick_sweep_winner(candidates)
        if winner is None:
            return CellOutcome(strategy_class.__name__, cell_key, "skipped:no_valid_candidate")
        best_params, _ = winner
        test_result = run_backtest(
            candles, strategy_class(**best_params),
            record_start_mts=test_start_mts,
        )
        return CellOutcome(
            strategy_class.__name__, cell_key, "ok",
            params=best_params,
            oos_net=test_result.net_monthly_return_pct,
            oos_max_dd=test_result.max_drawdown_pct,
            oos_fill_rate=test_result.fill_rate,
            oos_sortino=test_result.sortino,
            baseline_net=baseline_net,
        )
    except Exception:
        return CellOutcome(strategy_class.__name__, cell_key, "errored")
```

The script then loops over (symbol, period_agg), loads candles, computes baseline, calls `run_cell` per strategy. (Refactor `scripts/run_phase3b_matrix.py` accordingly — replace inline try/except block with `run_cell` calls.)

Add integration test:

```python
# Append to backend_py/tests/modules/backtest/test_matrix.py
from decimal import Decimal

from bfx_funding_bot.modules.backtest.matrix import run_cell
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _synthetic_candles(n: int = 100) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1704067200000 + i * 3_600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_run_cell_with_always_frr_returns_ok_outcome() -> None:
    # AlwaysFRR has no param_grid_for_cell → raises → "errored" status
    out = run_cell(
        AlwaysFRRStrategy, _synthetic_candles(100),
        eda_cell={}, cell_key="fUST_p2",
        baseline_net=Decimal("0.0"),
    )
    assert out.status == "errored"  # AlwaysFRR doesn't implement param_grid_for_cell


def test_run_cell_with_phase3b_strategy_returns_outcome() -> None:
    from bfx_funding_bot.modules.backtest.strategies.weekend_premium import (
        WeekendPremiumStrategy,
    )
    out = run_cell(
        WeekendPremiumStrategy, _synthetic_candles(100),
        eda_cell={"weekend_effect_size": Decimal("0.08")},
        cell_key="fUST_p2",
        baseline_net=Decimal("0.30"),
    )
    # Constant-rate candles → sweep should pick a winner; status must be ok or
    # skipped:no_valid_candidate (depending on fill_rate gates).
    assert out.status in ("ok", "skipped:no_valid_candidate")
```

- [ ] **Step 7: Run mypy + ruff + full suite**

Run: `cd backend_py && uv run mypy src/ scripts/run_phase3b_matrix.py && uv run ruff check && uv run pytest -m "not integration" -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/matrix.py \
        backend_py/scripts/run_phase3b_matrix.py \
        backend_py/tests/modules/backtest/test_matrix.py
git commit -m "$(cat <<'EOF'
✨ Feat: Phase 3b matrix runner + decision-rule helpers

Pure-function matrix.py: pick_sweep_winner (Sortino + +inf tie-break
+ health gates) + evaluate_strategy_consistency (4/6 cells + margin >5%).
Script run_phase3b_matrix.py orchestrates per-cell sweep on train portion,
OOS eval, baseline comparison, and prints per-strategy verdict.

EDA inputs come from a JSON file derived from the EDA report.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Run matrix on Neon + write Phase 3b result + Phase 3c decision

**Files:**
- Create: `docs/research/2026-05-17-phase3b-results.md`
- (Possibly) `/tmp/phase3b_eda.json` (transient; produced from EDA report)

**Why:** Closing the loop. Results report ends Phase 3b and sets up Phase 3c decision.

- [ ] **Step 1: Build EDA JSON from the EDA report**

Translate the values committed in `docs/research/2026-05-17-phase3b-eda.md` into a JSON file structured as:

```json
{
  "fUSD_p2": {
    "acf_168h_pass": true,
    "close_over_ema_sigma_24": "0.0532",
    "close_over_ema_sigma_168": "0.0789",
    "weekend_effect_size": "0.061"
  },
  "fUSD_p30": { ... },
  "...": { ... }
}
```

Save to `/tmp/phase3b_eda.json` (transient, not committed).

- [ ] **Step 2: Run the matrix script**

Run: `cd backend_py && uv run python scripts/run_phase3b_matrix.py --eda /tmp/phase3b_eda.json > /tmp/phase3b_results.txt && cat /tmp/phase3b_results.txt`

Expected: 6 cell sections + decision verdicts for 3 strategies.

- [ ] **Step 3: Write the results report**

Create `docs/research/2026-05-17-phase3b-results.md` following the structure from spec section "Output Format":

```markdown
# Phase 3b Results

## TL;DR
<one line: which strategies qualify; standout cells>

## Methodology Snapshot
- Window: post-2022-01-01
- Train/test split: 70/30 per cell
- Sweep metric: Sortino + fill_rate ≥ 0.3 + n_trades ≥ 10 floors
- Decision rule: consistency ≥ 4/6 + margin > 5% + health gates
- Conclusion scope: conditional on test-window regime; Phase 4 requires WFO

## OOS Comparison Table

| Cell | Strategy | Best params | Net %/mo | Max DD % | Fill rate | Sortino | vs Baseline |
|---|---|---|---|---|---|---|---|
... (fill from matrix script output)

## Per-cell Detail Appendix
<for each cell, summarize train sweep table + chosen winner rationale + OOS interpretation>

## Decision
- Strategies qualifying for Phase 4 candidate pool: <list>
- Strategies not qualifying + reasons: <list>
- Phase 3c launch (yes/no): <decision per spec rule>
```

- [ ] **Step 4: Update wiki + commit**

Update `~/second-brain/wiki/projects/bfx-funding-bot/index.md` Current Status + Recent Activity + Pending sections to reflect Phase 3b outcome.

```bash
git add docs/research/2026-05-17-phase3b-results.md
git commit -m "$(cat <<'EOF'
📝 Docs: Phase 3b results + Phase 3c decision

OOS matrix across 3 strategies × 6 cells. Decision verdicts per
consistency (≥4/6 cells) + margin (>5%) rule. Phase 3c launch
decided per spec rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Then in `~/second-brain/`:

```bash
cd ~/second-brain
# Update wiki/projects/bfx-funding-bot/index.md
git commit -m "daily: 2026-05-17 bfx Phase 3b matrix shipped + Phase 3c decision"
```

---

## Total estimate

**11 tasks → 9 commits in bfx-funding-bot + 1 commit in second-brain.** Matches spec estimate (8-9 commits, with 1-2 slot reserve for plan deviation per Phase 2 lesson).

Implementation only — does not include real-money safety review, observability, or worker model (deferred to v2 post-strategy phase).
