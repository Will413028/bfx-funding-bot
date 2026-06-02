# Adaptive-Period Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-on `AdaptivePeriodStrategy` that lends at `candle.close` and selects `period_days` from the rate's deviation-from-EMA (high→lock long, at/below→lock short), fully unit-tested and OOS-characterized — with zero impact on the live MeanReversion canary.

**Architecture:** New `Strategy` subclass mirroring `MeanReversionStrategy` (same EMA + EDA `ratio_sigma`), but mapping deviation to a *lock period* instead of a lend/pause gate. Slots into the shared backtest+live path via the `StrategyName` enum, a `AdaptivePeriodParams` validator, and a `build_strategy` dispatch branch — no engine/harness changes. Characterization runs through the existing `run_oos_profitability.py` against a dedicated `configs/cells.experimental.yaml` (canary config untouched).

**Tech Stack:** Python 3.13, Pydantic v2, Decimal arithmetic, pytest. All commands run from `backend_py/` via `uv`.

**Spec:** `docs/superpowers/specs/2026-06-03-adaptive-period-strategy-design.md`

---

## File Structure

- **Create** `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py` — the strategy class (one responsibility: deviation→period decisions).
- **Modify** `backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py` — add `ADAPTIVE_PERIOD` enum member.
- **Modify** `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py` — add `AdaptivePeriodParams` + validator branch.
- **Modify** `backend_py/src/bfx_funding_bot/modules/marketfeed/strategy_registry.py` — add `build_strategy` dispatch branch.
- **Modify** `backend_py/scripts/run_oos_profitability.py` — add `--cells` arg (default unchanged).
- **Create** `backend_py/configs/cells.experimental.yaml` — adaptive_period characterization cells (not deployed, not in the MR drift gate).
- **Create** `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py` — unit tests.
- **Modify** `backend_py/tests/modules/marketfeed/test_config.py` (or create if absent) — params validation test (see Task 1).
- **Create** `docs/research/2026-06-03-adaptive-period-characterization.md` — the characterization deliverable (Task 7).

All `period`/clamp constants live in `adaptive_period.py`.

---

### Task 1: StrategyName enum + AdaptivePeriodParams validator

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py:30-32`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py:13` (imports), `:37-47` (params classes), `:68-76` (validator)
- Test: `backend_py/tests/modules/marketfeed/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `backend_py/tests/modules/marketfeed/test_config.py` (create the file with these imports if it does not exist):

```python
import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName


def _ap_params() -> dict:
    return {"ema_span": 24, "ratio_sigma": 0.42, "t1": 0.5, "t2": 1.5, "p_mid": 7, "p_long": 30}


def test_adaptive_period_enum_value() -> None:
    assert StrategyName.ADAPTIVE_PERIOD.value == "adaptive_period"


def test_cellconfig_accepts_valid_adaptive_period_params() -> None:
    c = CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=_ap_params())
    assert c.pair_id == "adaptive_period:fUST_a30"


def test_cellconfig_rejects_t2_not_greater_than_t1() -> None:
    bad = _ap_params() | {"t1": 1.5, "t2": 1.5}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)


def test_cellconfig_rejects_p_long_less_than_p_mid() -> None:
    bad = _ap_params() | {"p_mid": 30, "p_long": 7}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)


def test_cellconfig_rejects_extra_param_key() -> None:
    bad = _ap_params() | {"bogus": 1}
    with pytest.raises(ValidationError):
        CellConfig(strategy="adaptive_period", symbol="fUST", period_agg="a30", params=bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config.py -k adaptive_period -v`
Expected: FAIL — `AttributeError: ADAPTIVE_PERIOD` (enum member missing).

- [ ] **Step 3: Add the enum member**

In `backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py`, change the `StrategyName` class (around line 30) to:

```python
class StrategyName(StrEnum):
    RATE_PERCENTILE = "rate_percentile"
    MEAN_REVERSION = "mean_reversion"
    ADAPTIVE_PERIOD = "adaptive_period"
```

- [ ] **Step 4: Add the params validator**

In `backend_py/src/bfx_funding_bot/modules/marketfeed/config.py`, update the pydantic import on line 13:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
```

Add this class immediately after `RatePercentileParams` (after line 47):

```python
class AdaptivePeriodParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ema_span: int = Field(ge=1)
    ratio_sigma: float = Field(gt=0)
    t1: float = Field(ge=0)
    t2: float = Field(gt=0)
    p_mid: int = Field(ge=2, le=120)
    p_long: int = Field(ge=2, le=120)

    @model_validator(mode="after")
    def _check_ordering(self) -> "AdaptivePeriodParams":
        if not self.t2 > self.t1:
            raise ValueError("t2 must be > t1")
        if not self.p_long >= self.p_mid:
            raise ValueError("p_long must be >= p_mid")
        return self
```

Add a branch to `CellConfig._validate_params` (after the `RATE_PERCENTILE` branch, around line 75):

```python
        elif strat == StrategyName.ADAPTIVE_PERIOD:
            AdaptivePeriodParams.model_validate(v)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config.py -k adaptive_period -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/schemas.py backend_py/src/bfx_funding_bot/modules/marketfeed/config.py backend_py/tests/modules/marketfeed/test_config.py
git commit -m "✨ Feat: add adaptive_period StrategyName + params validator"
```

---

### Task 2: AdaptivePeriodStrategy — construction, name, observe, getters

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _c(mts: int, close: str) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts,
        open=Decimal(close), close=Decimal(close),
        high=Decimal(close), low=Decimal(close),
        volume=Decimal("100"),
    )


def _ap(ema_span: int = 24, t1: str = "0.5", t2: str = "1.5",
        ratio_sigma: str = "0.05", p_mid: int = 7, p_long: int = 30) -> AdaptivePeriodStrategy:
    return AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=Decimal(ratio_sigma),
        t1=Decimal(t1), t2=Decimal(t2), p_mid=p_mid, p_long=p_long,
    )


def test_name_includes_params() -> None:
    assert _ap().name == "adaptive_period_ema24_t0.5_1.5"


def test_ema_current_none_before_first_observe() -> None:
    assert _ap().ema_current is None


def test_observe_seeds_then_tracks_ema() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    assert s.ema_current == Decimal("0.0003")  # first observe seeds
    s.observe(_c(3600_000, "0.0005"))
    alpha = Decimal(2) / Decimal(24 + 1)
    expected = alpha * Decimal("0.0005") + (Decimal("1") - alpha) * Decimal("0.0003")
    assert s.ema_current == expected


def test_observe_skips_none_close_and_does_not_count_sample() -> None:
    s = _ap()
    s.observe(_c(0, "0.0003"))
    none_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=1,
        open=None, close=None, high=None, low=None, volume=None,
    )
    s.observe(none_candle)
    assert s.samples == 1
    assert s.ema_current == Decimal("0.0003")


def test_window_filled_after_ema_span_samples() -> None:
    s = _ap(ema_span=3)
    assert not s.window_filled
    for i in range(3):
        s.observe(_c(i, "0.0003"))
    assert s.window_filled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -v`
Expected: FAIL — `ModuleNotFoundError: ...adaptive_period`.

- [ ] **Step 3: Create the strategy (construction + observe + getters only)**

Create `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py`:

```python
from __future__ import annotations

from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_PERIOD_FLOOR = 2       # Bitfinex funding offer minimum period (days)
_PERIOD_MAX = 120       # Bitfinex funding offer maximum period (days)


class AdaptivePeriodStrategy(Strategy):
    """Always lend at the market rate (candle.close); vary the lock PERIOD by
    the rate's deviation from its EMA.

    Same mean-reversion thesis as MeanReversion, expressed through duration
    instead of gating: rate high vs trend -> expected to revert down -> lock
    LONG (ride the locked-high rate down); rate at/below trend -> lock SHORT
    (re-price upward frequently). A spike (deviation > band2) is the top tier
    -> lock longest. Never pauses (maximizes bot-vs-idle).

    The fill model pins the optimal posting rate to candle.close, so rate is
    always candle.close; period is the only alpha lever, and the one no other
    strategy uses.
    """

    def __init__(
        self,
        ema_span: int,
        ratio_sigma: Decimal,
        t1: Decimal,
        t2: Decimal,
        p_mid: int,
        p_long: int,
    ) -> None:
        self._ema_span = ema_span
        self._ratio_sigma = ratio_sigma
        self._t1 = t1
        self._t2 = t2
        self._p_mid = p_mid
        self._p_long = p_long
        self._alpha = Decimal(2) / Decimal(ema_span + 1)
        self._ema: Decimal | None = None
        self._samples = 0
        self._last_period: int | None = None

    @property
    def name(self) -> str:
        return f"adaptive_period_ema{self._ema_span}_t{self._t1}_{self._t2}"

    @property
    def ema_current(self) -> Decimal | None:
        return self._ema

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def window_filled(self) -> bool:
        return self._samples >= self._ema_span

    @property
    def last_period(self) -> int | None:
        return self._last_period

    def observe(self, candle: FundingCandle) -> None:
        if candle.close is None:
            return
        self._samples += 1
        if self._ema is None:
            self._ema = candle.close
        else:
            self._ema = (
                self._alpha * candle.close
                + (Decimal("1") - self._alpha) * self._ema
            )

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        raise NotImplementedError  # implemented in Task 3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py backend_py/tests/modules/backtest/strategies/test_adaptive_period.py
git commit -m "✨ Feat: AdaptivePeriodStrategy construction + observe + getters"
```

---

### Task 3: decide() — deviation→period mapping

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`

Mapping (with `band1 = t1*ratio_sigma`, `band2 = t2*ratio_sigma`): not-warmed→2, `deviation ≤ band1`→2, `band1 < deviation ≤ band2`→`p_mid`, `deviation > band2`→`p_long`; result clamped to `[2,120]`; `close is None`→`None`.

- [ ] **Step 1: Write the failing tests**

Append to `test_adaptive_period.py`:

```python
def _warm(s: AdaptivePeriodStrategy, level: str, n: int = 30) -> None:
    """Warm EMA to `level` with n samples so window_filled and ema≈level."""
    for i in range(n):
        s.observe(_c(i, level))


def test_decide_returns_none_when_close_is_none() -> None:
    s = _ap()
    none_candle = FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=5,
        open=None, close=None, high=None, low=None, volume=None,
    )
    assert s.decide(none_candle) is None


def test_decide_period_floor_during_warmup() -> None:
    s = _ap(ema_span=24)
    s.observe(_c(0, "0.0001"))          # 1 sample << ema_span -> not warmed
    cand = _c(1, "0.0005")              # big positive deviation, but warmup
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None
    assert d.period_days == 2
    assert d.rate == Decimal("0.0005")  # always lends at market


def test_decide_floor_when_at_or_below_trend() -> None:
    # warmed flat at 0.0010, then a candle equal to EMA -> deviation 0 <= band1
    s = _ap(ema_span=3, t1="0.5", t2="1.5", ratio_sigma="0.10")
    _warm(s, "0.0010", n=5)
    cand = _c(100, "0.0010")
    s.observe(cand)
    d = s.decide(cand)
    assert d is not None and d.period_days == 2


def test_decide_p_mid_when_mildly_above_trend() -> None:
    # ema_span=1 -> alpha=1.0 -> ema == previous close exactly (easy goldens).
    # Warm to 0.0010, then decide on a candle whose deviation lands in (band1, band2].
    # band1 = 0.5*0.10 = 0.05 ; band2 = 1.5*0.10 = 0.15
    # want deviation in (0.05, 0.15]: close=0.0011 over ema=0.0010 -> dev=0.10
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    s.observe(_c(0, "0.0010"))          # ema=0.0010 (seed), samples=1>=ema_span=1
    cand = _c(1, "0.0011")
    # do NOT observe cand before decide, so ema stays 0.0010 for a clean golden
    d = s.decide(cand)
    assert d is not None and d.period_days == 7


def test_decide_p_long_on_spike() -> None:
    # deviation = 0.20 > band2 (0.15) -> p_long
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=30)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")              # dev = 0.20
    d = s.decide(cand)
    assert d is not None and d.period_days == 30


def test_decide_boundary_band1_inclusive_to_floor() -> None:
    # deviation exactly == band1 (0.05) -> floor (<=)
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.00105")             # dev = 0.05 == band1
    d = s.decide(cand)
    assert d is not None and d.period_days == 2


def test_decide_boundary_band2_inclusive_to_mid() -> None:
    # deviation exactly == band2 (0.15) -> p_mid (<=)
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.00115")             # dev = 0.15 == band2
    d = s.decide(cand)
    assert d is not None and d.period_days == 7


def test_decide_clamps_p_long_above_max() -> None:
    s = _ap(ema_span=1, t1="0.5", t2="1.5", ratio_sigma="0.10", p_mid=7, p_long=999)
    s.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")              # spike tier
    d = s.decide(cand)
    assert d is not None and d.period_days == 120  # clamped to Bitfinex max


def test_decide_sets_last_period() -> None:
    s = _ap(ema_span=1, ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    s.decide(_c(1, "0.0012"))
    assert s.last_period == 30


def test_decide_rate_always_equals_close() -> None:
    s = _ap(ema_span=1, ratio_sigma="0.10")
    s.observe(_c(0, "0.0010"))
    for close in ("0.0009", "0.0011", "0.0012"):
        cand = _c(2, close)
        d = s.decide(cand)
        assert d is not None and d.rate == Decimal(close)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k decide -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement decide()**

In `adaptive_period.py`, replace the `decide` stub with a private mapping helper + `decide`:

```python
    def _period_for(self, close: Decimal) -> int:
        # Warmup or undefined EMA -> safest shortest lock.
        if self._ema is None or self._ema == 0 or self._samples < self._ema_span:
            return _PERIOD_FLOOR
        deviation = (close - self._ema) / self._ema
        band1 = self._t1 * self._ratio_sigma
        band2 = self._t2 * self._ratio_sigma
        if deviation <= band1:
            period = _PERIOD_FLOOR
        elif deviation <= band2:
            period = self._p_mid
        else:
            period = self._p_long
        return max(_PERIOD_FLOOR, min(_PERIOD_MAX, period))

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        period = self._period_for(candle.close)
        self._last_period = period
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=period)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py backend_py/tests/modules/backtest/strategies/test_adaptive_period.py
git commit -m "✨ Feat: AdaptivePeriodStrategy.decide deviation->period mapping"
```

---

### Task 4: param_grid_for_cell

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py`
- Test: `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`

- [ ] **Step 1: Write the failing test**

Append to `test_adaptive_period.py`:

```python
def test_param_grid_for_cell_from_eda() -> None:
    grid = AdaptivePeriodStrategy.param_grid_for_cell(
        symbol="fUST", period_agg="p2",
        eda={
            "close_over_ema_sigma_24": Decimal("0.05"),
            "close_over_ema_sigma_168": Decimal("0.08"),
        },
    )
    assert len(grid) == 4  # 2 ema_spans x 2 (t1,t2) pairs
    assert {p["ema_span"] for p in grid} == {24, 168}
    assert {(p["t1"], p["t2"]) for p in grid} == {
        (Decimal("0.5"), Decimal("1.5")),
        (Decimal("1.0"), Decimal("2.0")),
    }
    for p in grid:
        assert p["p_mid"] == 7 and p["p_long"] == 30
        assert p["ratio_sigma"] == (Decimal("0.05") if p["ema_span"] == 24 else Decimal("0.08"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k param_grid -v`
Expected: FAIL — `NotImplementedError: AdaptivePeriodStrategy does not implement param_grid_for_cell` (inherited from base).

- [ ] **Step 3: Implement param_grid_for_cell**

Add to `AdaptivePeriodStrategy` (after `decide`):

```python
    @classmethod
    def param_grid_for_cell(
        cls, symbol: str, period_agg: str, eda: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Grid: ema_span in {24, 168} x (t1, t2) in {(0.5,1.5),(1.0,2.0)}.
        Period tiers fixed (p_mid=7, p_long=30) for v1; ratio_sigma injected
        per ema_span from EDA close/EMA sigma stats (same as MeanReversion).
        """
        sigma_24 = eda.get("close_over_ema_sigma_24", Decimal("0.05"))
        sigma_168 = eda.get("close_over_ema_sigma_168", Decimal("0.05"))
        spans = [(24, sigma_24), (168, sigma_168)]
        thresholds = [
            (Decimal("0.5"), Decimal("1.5")),
            (Decimal("1.0"), Decimal("2.0")),
        ]
        return [
            {
                "ema_span": span, "ratio_sigma": sigma,
                "t1": t1, "t2": t2, "p_mid": 7, "p_long": 30,
            }
            for span, sigma in spans
            for (t1, t2) in thresholds
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k param_grid -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py backend_py/tests/modules/backtest/strategies/test_adaptive_period.py
git commit -m "✨ Feat: AdaptivePeriodStrategy.param_grid_for_cell"
```

---

### Task 5: build_strategy dispatch

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/strategy_registry.py:15-25` (imports), `:41-56` (dispatch)
- Test: `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`

- [ ] **Step 1: Write the failing test**

Append to `test_adaptive_period.py`:

```python
def test_build_strategy_from_cellconfig() -> None:
    from bfx_funding_bot.modules.marketfeed.config import CellConfig
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

    cell = CellConfig(
        strategy="adaptive_period", symbol="fUST", period_agg="a30",
        params={"ema_span": 24, "ratio_sigma": 0.42, "t1": 0.5, "t2": 1.5,
                "p_mid": 7, "p_long": 30},
    )
    s = build_strategy(cell)
    assert isinstance(s, AdaptivePeriodStrategy)
    assert s.name == "adaptive_period_ema24_t0.5_1.5"


def test_build_strategy_is_deterministic() -> None:
    from bfx_funding_bot.modules.marketfeed.config import CellConfig
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

    cell = CellConfig(
        strategy="adaptive_period", symbol="fUST", period_agg="a30",
        params={"ema_span": 1, "ratio_sigma": 0.10, "t1": 0.5, "t2": 1.5,
                "p_mid": 7, "p_long": 30},
    )
    a, b = build_strategy(cell), build_strategy(cell)
    a.observe(_c(0, "0.0010")); b.observe(_c(0, "0.0010"))
    cand = _c(1, "0.0012")
    da, db = a.decide(cand), b.decide(cand)
    assert da is not None and db is not None
    assert da.period_days == db.period_days == 30
    assert da.rate == db.rate
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k build_strategy -v`
Expected: FAIL — `ValueError: unsupported strategy <StrategyName.ADAPTIVE_PERIOD>`.

- [ ] **Step 3: Add the import + dispatch branch**

In `strategy_registry.py`, add the import after the `rate_percentile` import (after line 21):

```python
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import (
    AdaptivePeriodStrategy,
)
```

Add this branch to `build_strategy`, before the final `raise` (after the `RATE_PERCENTILE` branch, line 55):

```python
    if cell.strategy == StrategyName.ADAPTIVE_PERIOD:
        p = cell.params
        return AdaptivePeriodStrategy(
            ema_span=int(p["ema_span"]),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
            t1=Decimal(str(p["t1"])),
            t2=Decimal(str(p["t2"])),
            p_mid=int(p["p_mid"]),
            p_long=int(p["p_long"]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k build_strategy -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/strategy_registry.py backend_py/tests/modules/backtest/strategies/test_adaptive_period.py
git commit -m "✨ Feat: build_strategy dispatch for adaptive_period"
```

---

### Task 6: --cells arg + experimental cells config

**Files:**
- Modify: `backend_py/scripts/run_oos_profitability.py:56` (CANARY_YAML), `:239-242` (argparse), `:253` (load_cells_only)
- Create: `backend_py/configs/cells.experimental.yaml`
- Test: `backend_py/tests/modules/backtest/strategies/test_adaptive_period.py`

- [ ] **Step 1: Write the failing test**

Append to `test_adaptive_period.py`:

```python
def test_experimental_cells_yaml_loads_and_builds() -> None:
    from pathlib import Path
    from bfx_funding_bot.modules.marketfeed.config import load_cells_only
    from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

    cells = load_cells_only(Path("configs/cells.experimental.yaml"))
    assert len(cells) == 4
    assert all(c.strategy.value == "adaptive_period" for c in cells)
    assert {c.cell_id for c in cells} == {"fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2"}
    for c in cells:
        s = build_strategy(c)
        assert s.name.startswith("adaptive_period_")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k experimental -v`
Expected: FAIL — `FileNotFoundError: configs/cells.experimental.yaml`.

- [ ] **Step 3: Create the experimental cells config**

Create `backend_py/configs/cells.experimental.yaml`:

```yaml
# Phase 3c EXPERIMENTAL cells — AdaptivePeriodStrategy characterization ONLY.
#
# NOT deployed. NOT covered by derive_cells --check (that gate is MeanReversion-
# specific; see cell_pipeline.check_against_fixture). Used by:
#   uv run python scripts/run_oos_profitability.py \
#     --cells configs/cells.experimental.yaml --n-trials 4 --output ...
# to compare adaptive-period vs AlwaysMarketRate (period alpha) and vs the
# MeanReversion canary reports (bot-vs-idle), over the same OOS windows.
#
# ratio_sigma copied verbatim from configs/cells.canary.yaml (it is the EDA
# close_over_ema_sigma_24 — strategy-independent). Period tiers fixed for v1.
cells:
  - strategy: adaptive_period
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {ema_span: 24, ratio_sigma: 0.42049266874194213, t1: 0.5, t2: 1.5, p_mid: 7, p_long: 30}

  - strategy: adaptive_period
    symbol: fUST
    period_agg: p2
    timeframe: 1h
    params: {ema_span: 24, ratio_sigma: 0.4062263004196277, t1: 0.5, t2: 1.5, p_mid: 7, p_long: 30}

  - strategy: adaptive_period
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {ema_span: 24, ratio_sigma: 0.3766563427150627, t1: 0.5, t2: 1.5, p_mid: 7, p_long: 30}

  - strategy: adaptive_period
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {ema_span: 24, ratio_sigma: 0.3450137927640065, t1: 0.5, t2: 1.5, p_mid: 7, p_long: 30}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_adaptive_period.py -k experimental -v`
Expected: PASS.

- [ ] **Step 5: Add the `--cells` arg to the OOS script**

In `backend_py/scripts/run_oos_profitability.py`, change line 56 from:

```python
CANARY_YAML = Path("configs/cells.canary.yaml")
```

to:

```python
DEFAULT_CELLS_YAML = Path("configs/cells.canary.yaml")
```

In `_amain` (around lines 239-242), add the arg after the `--n-trials` arg:

```python
    parser.add_argument(
        "--cells", default=str(DEFAULT_CELLS_YAML),
        help="cells yaml path (default: configs/cells.canary.yaml)",
    )
```

Change the cell-loading loop (line 253) from:

```python
        for cell in load_cells_only(CANARY_YAML):
```

to:

```python
        for cell in load_cells_only(Path(args.cells)):
```

- [ ] **Step 6: Run test to verify the script still imports/parses**

Run: `cd backend_py && uv run python scripts/run_oos_profitability.py --help`
Expected: usage text listing `--cells` (no traceback).

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/scripts/run_oos_profitability.py backend_py/configs/cells.experimental.yaml backend_py/tests/modules/backtest/strategies/test_adaptive_period.py
git commit -m "✨ Feat: --cells arg + experimental adaptive_period cells config"
```

---

### Task 7: Characterize via OOS harness + write report

**Files:**
- Create: `docs/research/2026-06-03-adaptive-period-recent.md` (+ `.json`, auto)
- Create: `docs/research/2026-06-03-adaptive-period-fullhistory.md` (+ `.json`, auto)
- Create: `docs/research/2026-06-03-adaptive-period-characterization.md` (the analysis deliverable)

> This task runs the existing OOS harness against the experimental cells and
> writes the analysis. `.env` points at live Neon (read-only candle queries here).
> If the Neon password has rotated, refresh it via `mcp__Neon__get_connection_string`
> before running (per project notes). No DB writes.

- [ ] **Step 1: Full unit gate before characterizing**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: all tests pass, `mypy ... Success`, ruff clean. (Do not characterize on a red build.)

- [ ] **Step 2: Run the recent-window (2022→now) characterization**

Run:
```bash
cd backend_py && uv run python scripts/run_oos_profitability.py \
  --cells configs/cells.experimental.yaml --n-trials 4 \
  --output ../docs/research/2026-06-03-adaptive-period-recent.md
```
Expected: logs `fUST_a30: N windows` … for 4 cells; writes the `.md` + `.json`.

- [ ] **Step 3: Run the full-history characterization (temporary START_MTS override)**

The full-history start is a manual, uncommitted override (mirrors the 2026-06-02 MR full-history run). In `scripts/run_oos_profitability.py`, temporarily change line 54:

```python
START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
```
to:
```python
START_MTS = int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000)
```

Run:
```bash
cd backend_py && uv run python scripts/run_oos_profitability.py \
  --cells configs/cells.experimental.yaml --n-trials 4 \
  --output ../docs/research/2026-06-03-adaptive-period-fullhistory.md
```

Then **revert the override (do NOT commit it):**
```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot && git checkout backend_py/scripts/run_oos_profitability.py
```
Verify: `git diff --stat backend_py/scripts/run_oos_profitability.py` prints nothing.

- [ ] **Step 4: Write the characterization analysis**

Create `docs/research/2026-06-03-adaptive-period-characterization.md`. It MUST contain:

1. **Header**: title, run date, config (`cells.experimental.yaml`), params (ema_span 24, t1 0.5, t2 1.5, tiers 2/7/30), fill-model + OOS caveats (copy the caveat language from the MR reports).
2. **Comparison table** — for each of the 4 cells, three rows of bot-vs-idle annualized %:
   - AdaptivePeriod (from this run's strategy column)
   - MeanReversion (from `2026-06-02-oos-full-history-profitability.md` for full; `2026-06-03-oos-recent-window.md` for recent)
   - AlwaysMarketRate (the baseline column in this run's report)
   for BOTH windows (recent 2022→, full 2016→).
3. **Period-alpha verdict** — adaptive_period's active-return vs AlwaysMarketRate(period=2) section (median active %/mo, IR, win-rate, deflated-Sharpe). State plainly: does varying the lock period beat always-2d at the same market rate, and in which regime?
4. **Period-distribution note** — comment on how often each tier (2/7/30) fired (inferable from the report's behavior; if the report does not expose it, state that and note it as a follow-up instrumentation item). Flag the 1-month-window vs 30-day-lock interaction (a `p_long=30` lock can span almost a whole OOS window → few decisions/window).
5. **Takeaway** — per the spec's framing: bot-vs-idle is the durable headline; period alpha is the bonus under test. Recommend whether to pursue deployment wiring (Task §7 of the spec) or iterate params first.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/research/2026-06-03-adaptive-period-recent.md docs/research/2026-06-03-adaptive-period-recent.json docs/research/2026-06-03-adaptive-period-fullhistory.md docs/research/2026-06-03-adaptive-period-fullhistory.json docs/research/2026-06-03-adaptive-period-characterization.md
git commit -m "📝 Docs: adaptive-period OOS characterization (recent + full history)"
```

---

## Final Verification

- [ ] `cd backend_py && uv run pytest -m "not integration" -q` — all green (was 1114+; now includes the new `test_adaptive_period.py` + `test_config.py` additions).
- [ ] `cd backend_py && uv run mypy src/` — Success.
- [ ] `cd backend_py && uv run ruff check` — clean.
- [ ] `git diff --stat backend_py/scripts/run_oos_profitability.py` against HEAD — only the `--cells`/`DEFAULT_CELLS_YAML` changes are committed; the START_MTS override is NOT present.
- [ ] `git status` — no stray uncommitted edits; `cells.canary.yaml` unchanged (`git diff backend_py/configs/cells.canary.yaml` empty).
- [ ] Confirm the live canary is untouched: no changes under `configs/cells.canary.yaml`, no deploy step run.

---

## Self-Review Notes (author checklist — done)

- **Spec coverage**: §3 mechanism → Tasks 2-3; §4 interface → Tasks 1,5; §5 grid → Task 4; §6 characterization → Tasks 6-7; §9 test cases → all covered (warmup, tiers, boundaries, clamp, None, name, grid, registry, determinism). §7 (live deploy) + divergence handling are explicitly deferred (out of scope, not tasked).
- **No placeholders**: every code step shows full code; every command shows expected output.
- **Type consistency**: ctor signature `(ema_span, ratio_sigma, t1, t2, p_mid, p_long)` is identical in Task 2 (class), Task 5 (build_strategy), Task 6 (yaml keys), and all tests. `name` format `adaptive_period_ema{span}_t{t1}_{t2}` consistent across Tasks 2/5. Params field names (`t1,t2,p_mid,p_long,ema_span,ratio_sigma`) identical in Task 1 validator, Task 5 dispatch, Task 6 yaml.
- **Deferred (not tasked, by design)**: derive_cells sweep/--check wiring, cells.canary.yaml entry, live deploy, divergence_reporter period-boundary handling — all gated on favorable characterization + live G3 (spec §2, §7, §11).
