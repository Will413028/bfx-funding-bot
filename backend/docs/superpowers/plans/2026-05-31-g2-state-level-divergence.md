# G2 State-level Divergence Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift live-vs-replay divergence detection from action-parity to state-parity by exposing the strategies' decision-determining accumulators and feeding them (Decimal-precise) into the divergence vector, plus a continuous `signal_score`.

**Architecture:** Add read-only `@property` accessors over existing strategy state (MR `_ema`/deviation, RP threshold/window-filled), caching the value `decide()` already computes — no change to `decide()`/`observe()` behavior. The `divergence_reporter` then includes these fields in `_strategy_attributes` (so `_diff_fields` flags any drift) and replaces the `±1.0` placeholder `signal_score` with a continuous state-function.

**Tech Stack:** Python 3.13, Decimal, numpy (existing), pytest + hypothesis. All commands run from `backend_py/` via `uv`.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py` | MR strategy | + `ema_current`, `last_deviation` props; cache deviation in `decide()` |
| `src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py` | RP strategy | + `last_threshold`, `window_filled` props; cache threshold in `decide()` |
| `src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py` | live-vs-replay diff | richer `_strategy_attributes`; continuous `_normalize_signal_score` |
| `tests/modules/backtest/strategies/test_mean_reversion.py` | MR tests | + property tests (create if absent) |
| `tests/modules/backtest/strategies/test_rate_percentile.py` | RP tests | + property tests (create if absent) |
| `tests/modules/marketfeed/test_divergence_reporter.py` | reporter tests | + state-drift detection + score tests |

Note: the strategy test files may not exist yet. Step 1 of Tasks 1/2 says to check; if absent, create with the import header shown.

---

### Task 1: MeanReversion — `ema_current` + `last_deviation` props

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py`
- Test: `tests/modules/backtest/strategies/test_mean_reversion.py`

- [ ] **Step 1: Write the failing tests**

Check whether `tests/modules/backtest/strategies/test_mean_reversion.py` exists. If not, create it with this content; if it exists, append the four test functions.

```python
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(mts: int, close: Decimal) -> FundingCandle:
    return FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="a30", mts=mts,
        open=close, close=close, high=close, low=close, volume=Decimal("100"),
    )


def _mr() -> MeanReversionStrategy:
    return MeanReversionStrategy(
        ema_span=24, threshold_sigma=Decimal("0.5"), ratio_sigma=Decimal("0.05")
    )


def test_ema_current_none_before_first_observe():
    assert _mr().ema_current is None


def test_ema_current_tracks_accumulator():
    mr = _mr()
    mr.observe(_candle(0, Decimal("0.0003")))
    # first observe seeds ema to the close
    assert mr.ema_current == Decimal("0.0003")
    mr.observe(_candle(3600_000, Decimal("0.0005")))
    alpha = Decimal(2) / Decimal(24 + 1)
    expected = alpha * Decimal("0.0005") + (Decimal("1") - alpha) * Decimal("0.0003")
    assert mr.ema_current == expected


def test_last_deviation_none_before_decide():
    mr = _mr()
    mr.observe(_candle(0, Decimal("0.0003")))
    assert mr.last_deviation is None


def test_last_deviation_caches_most_recent_decide():
    mr = _mr()
    mr.observe(_candle(0, Decimal("0.0003")))
    c = _candle(3600_000, Decimal("0.0004"))
    mr.observe(c)
    mr.decide(c)
    ema = mr.ema_current
    assert ema is not None
    assert mr.last_deviation == (Decimal("0.0004") - ema) / ema
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_mean_reversion.py -q`
Expected: FAIL (AttributeError: 'MeanReversionStrategy' object has no attribute 'ema_current')

- [ ] **Step 3: Implement the properties + deviation cache**

In `mean_reversion.py`, add `self._last_deviation` init and the props, and cache deviation in `decide()`. The `__init__` gains one line:

```python
        self._ema: Decimal | None = None
        self._last_deviation: Decimal | None = None
```

Add properties after the `name` property:

```python
    @property
    def ema_current(self) -> Decimal | None:
        return self._ema

    @property
    def last_deviation(self) -> Decimal | None:
        return self._last_deviation
```

Rewrite `decide()` to cache the deviation it already computes (behavior identical — same return value):

```python
    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None or self._ema is None or self._ema == 0:
            return None
        deviation = (candle.close - self._ema) / self._ema
        self._last_deviation = deviation
        lower_band = -self._threshold_sigma * self._ratio_sigma
        if deviation < lower_band:
            return None
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_mean_reversion.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the existing MR/backtest suites to confirm no behavior change**

Run: `cd backend_py && uv run pytest tests/modules/backtest -q`
Expected: PASS (all green — decide()/observe() behavior unchanged)

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py backend_py/tests/modules/backtest/strategies/test_mean_reversion.py
git commit -m "✨ Feat: MeanReversion ema_current/last_deviation observability props (G2 A)"
```

---

### Task 2: RatePercentile — `last_threshold` + `window_filled` props

**Files:**
- Modify: `src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py`
- Test: `tests/modules/backtest/strategies/test_rate_percentile.py`

- [ ] **Step 1: Write the failing tests**

Check whether `tests/modules/backtest/strategies/test_rate_percentile.py` exists. If not, create it with this content; if it exists, append the test functions.

```python
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.backtest.strategies.rate_percentile import (
    RatePercentileStrategy,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candle(mts: int, close: Decimal) -> FundingCandle:
    return FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="a30", mts=mts,
        open=close, close=close, high=close, low=close, volume=Decimal("100"),
    )


def _rp(percentile: int = 75, lookback: int = 3) -> RatePercentileStrategy:
    return RatePercentileStrategy(percentile=percentile, lookback_hours=lookback)


def test_window_filled_false_until_lookback_reached():
    rp = _rp(lookback=3)
    assert rp.window_filled is False
    rp.observe(_candle(0, Decimal("0.0001")))
    rp.observe(_candle(1, Decimal("0.0002")))
    assert rp.window_filled is False
    rp.observe(_candle(2, Decimal("0.0003")))
    assert rp.window_filled is True


def test_last_threshold_none_during_warmup():
    rp = _rp(lookback=3)
    rp.observe(_candle(0, Decimal("0.0001")))
    c = _candle(1, Decimal("0.0002"))
    rp.observe(c)
    rp.decide(c)  # window not full → decide returns early, no threshold cached
    assert rp.last_threshold is None


def test_last_threshold_caches_decide_value():
    rp = _rp(percentile=50, lookback=3)
    for i, v in enumerate(["0.0001", "0.0003", "0.0002"]):
        rp.observe(_candle(i, Decimal(v)))
    c = _candle(3, Decimal("0.0002"))
    rp.observe(c)  # window now [0.0003, 0.0002, 0.0002] (maxlen=3)
    rp.decide(c)
    assert rp.last_threshold is not None
    # 50th percentile of the current window
    import numpy as np
    win = [0.0003, 0.0002, 0.0002]
    expected = Decimal(str(float(np.percentile(win, 50))))
    assert rp.last_threshold == expected


def test_window_values_returns_current_window_snapshot():
    rp = _rp(lookback=3)
    rp.observe(_candle(0, Decimal("0.0001")))
    rp.observe(_candle(1, Decimal("0.0002")))
    assert rp.window_values == (Decimal("0.0001"), Decimal("0.0002"))
    # rolls with maxlen
    rp.observe(_candle(2, Decimal("0.0003")))
    rp.observe(_candle(3, Decimal("0.0004")))
    assert rp.window_values == (Decimal("0.0002"), Decimal("0.0003"), Decimal("0.0004"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_rate_percentile.py -q`
Expected: FAIL (AttributeError: 'RatePercentileStrategy' object has no attribute 'window_filled')

- [ ] **Step 3: Implement the properties + threshold cache**

In `rate_percentile.py`, add `self._last_threshold` init, the props, and cache the threshold in `decide()`. `__init__` gains one line:

```python
        self._window: deque[Decimal] = deque(maxlen=lookback_hours)
        self._last_threshold: Decimal | None = None
```

Add properties after the `name` property:

```python
    @property
    def last_threshold(self) -> Decimal | None:
        return self._last_threshold

    @property
    def window_filled(self) -> bool:
        return len(self._window) >= self._lookback_hours

    @property
    def window_values(self) -> tuple[Decimal, ...]:
        """Read-only snapshot of the rolling window (for parity diagnostics)."""
        return tuple(self._window)
```

Rewrite `decide()` to cache the threshold (behavior identical):

```python
    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        if len(self._window) < self._lookback_hours:
            return None  # warmup
        threshold = Decimal(str(float(
            np.percentile([float(x) for x in self._window], self._percentile)
        )))
        self._last_threshold = threshold
        if candle.close >= threshold:
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/strategies/test_rate_percentile.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the existing backtest suites to confirm no behavior change**

Run: `cd backend_py && uv run pytest tests/modules/backtest -q`
Expected: PASS (all green)

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/strategies/rate_percentile.py backend_py/tests/modules/backtest/strategies/test_rate_percentile.py
git commit -m "✨ Feat: RatePercentile last_threshold/window_filled observability props (G2 A)"
```

---

### Task 3: Reporter — include derived state in `_strategy_attributes` + headline drift test

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py:61-83` (`_strategy_attributes`)
- Test: `tests/modules/marketfeed/test_divergence_reporter.py`

- [ ] **Step 1: Write the failing headline test (state drift, same direction)**

Append to `tests/modules/marketfeed/test_divergence_reporter.py`. This is the bug the old direction-only reporter missed: a live signal whose direction MATCHES replay but whose MR `ema_current` differs must now be flagged.

```python
def test_state_drift_detected_even_when_direction_matches():
    """The headline G2 invariant: live and replay agree on direction (POST) but
    the MR ema accumulator silently drifted → must surface as strategy_attributes
    divergence. The old direction-only reporter missed this."""
    from bfx_funding_bot.modules.marketfeed.schemas import SignalDirection

    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    # Uniform rising history → replay computes POST with a specific ema.
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(10)]

    # Build a live_signal that AGREES on direction (POST) and attributes shape but
    # carries a drifted ema_current value (the silent-drift scenario).
    fake_live = ExtractedSignal(
        signal_score=0.0,
        signal_direction=SignalDirection.POST,
        strategy_attributes=tuple(sorted({
            "rate": 0.0003,
            "threshold_sigma": 0.5,
            "ema_current": Decimal("0.0009"),   # drifted vs the true ~0.0003
            "last_deviation": Decimal("0"),
        }.items())),
        lend_decision=None,
    )
    reporter = DivergenceReporter()
    result = reporter.check(
        cell=cell, raw_history=history, boundary_candle=history[-1],
        budget_hours=2, live_signal=fake_live,
    )
    assert result is not None, "state drift must be detected even with matching direction"
    assert "strategy_attributes" in result["diff_fields"]
    assert "signal_direction" not in result["diff_fields"]  # directions agreed
    # Pin the FEATURE (not an incidental key mismatch): replay must now EMIT the
    # real ema_current, and it must differ from the drifted live value.
    replay_attrs = dict(result["replay"]["strategy_attributes"])
    assert "ema_current" in replay_attrs, "replay must expose ema_current"
    assert Decimal(str(replay_attrs["ema_current"])) != Decimal("0.0009")
```

- [ ] **Step 2: Run test to verify it fails for the right reason**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_divergence_reporter.py::test_state_drift_detected_even_when_direction_matches -q`
Expected: FAIL on `assert "ema_current" in replay_attrs` — the current `_strategy_attributes` emits only `{rate, threshold_sigma}` for MR, so replay has no `ema_current` key. (The `is not None` line may pass incidentally because the fake's extra keys already make `strategy_attributes` differ; the `ema_current in replay_attrs` assertion is what pins the missing feature.)

- [ ] **Step 3: Implement richer `_strategy_attributes`**

Replace `_strategy_attributes` in `divergence_reporter.py`. Keep Decimal precision (do NOT cast the new state fields to float):

```python
def _strategy_attributes(
    cell: CellConfig, strategy: Any, candle: FundingCandle, ld: LendDecision | None,
) -> dict[str, Any]:
    """Per-strategy attribute extraction for divergence comparison.

    Includes decision-determining internal state (MR ema/deviation, RP
    threshold/window-filled) at Decimal precision so silent accumulator drift is
    detectable even when the resulting direction matches (G2 state-parity).
    Reads strategy state AFTER ExtractedSignal.extract has called observe+decide,
    so the cached deviation/threshold reflect this boundary candle.
    """
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        return {
            "percentile": float(cell.params["percentile"]),
            "last_close": float(candle.close) if candle.close is not None else 0.0,
            "last_threshold": strategy.last_threshold,
            "window_filled": strategy.window_filled,
        }
    if cell.strategy == StrategyName.MEAN_REVERSION:
        return {
            "rate": float(candle.close) if candle.close is not None else 0.0,
            "threshold_sigma": float(cell.params["threshold_sigma"]),
            "ema_current": strategy.ema_current,
            "last_deviation": strategy.last_deviation,
        }
    return {}
```

- [ ] **Step 4: Run the headline test + full reporter module**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_divergence_reporter.py -q`
Expected: PASS — the headline drift test passes AND the existing byte-equivalence tests (`test_no_divergence_when_inputs_match`, `test_replay_byte_equivalent_with_locf_on_sparse_input`, `test_cp1_byte_equivalence_property`) stay green (live and replay both emit the same real state, so still no false divergence).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py backend_py/tests/modules/marketfeed/test_divergence_reporter.py
git commit -m "✨ Feat: divergence reporter includes Decimal strategy state (G2 B)"
```

---

### Task 4: Reporter — continuous `_normalize_signal_score`

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py:86-95` (`_normalize_signal_score`)
- Test: `tests/modules/marketfeed/test_divergence_reporter.py`

- [ ] **Step 1: Write the failing score tests**

Append to `tests/modules/marketfeed/test_divergence_reporter.py`:

```python
def test_signal_score_mr_is_continuous_deviation():
    """MR signal_score is the continuous (close-ema)/ema margin, not ±1.0."""
    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    strat = build_strategy(cell)
    history = [_candle(1747584000000 + i * 3600_000, Decimal("0.0003"))
               for i in range(5)]
    for c in history[:-1]:
        strat.observe(c)
    sig = ExtractedSignal.extract(cell, strat, history[-1])
    # deviation for a uniform series is ~0 but score must equal float(last_deviation)
    assert sig.signal_score == float(strat.last_deviation)
    assert sig.signal_score != 1.0  # no longer the binary placeholder


def test_signal_score_rp_is_percentile_rank():
    """RP signal_score is the 0-100 rank of close within the window."""
    cell = _cell_rp()  # percentile=75, lookback=8
    # window: eight ascending closes; boundary close at the top → rank 100
    history = [_candle(1747584000000 + i * 3600_000, Decimal(f"0.000{i+1}"))
               for i in range(9)]
    strat = build_strategy(cell)
    for c in history[:-1]:
        strat.observe(c)
    sig = ExtractedSignal.extract(cell, strat, history[-1])
    # boundary close (0.0009) is >= every element observed into the window → 100.0
    assert sig.signal_score == 100.0


def test_signal_score_zero_when_state_unavailable():
    """MR with no usable ema (empty history) → score 0.0, no crash."""
    cell = CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.05},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    strat = build_strategy(cell)
    c = _candle(1747584000000, Decimal("0.0003"))
    sig = ExtractedSignal.extract(cell, strat, c)  # only one observe (via extract)
    # ema seeded this tick → deviation 0; score is float(0) == 0.0
    assert sig.signal_score == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_divergence_reporter.py -k signal_score -q`
Expected: FAIL — current `_normalize_signal_score` returns `1.0`/`-1.0`, so `signal_score == float(deviation)` and `== 100.0` fail.

- [ ] **Step 3: Implement the continuous score**

Replace `_normalize_signal_score` in `divergence_reporter.py`. Read the strategy's cached state (set by `extract`'s observe+decide before this is called):

```python
def _normalize_signal_score(
    cell: CellConfig, strategy: Any, candle: FundingCandle, ld: LendDecision | None,
) -> float:
    """Continuous, strategy-specific signal-strength score (replaces the old
    direction-only ±1.0 placeholder). Same-strategy live vs replay must be equal.

    - MR: the (close-ema)/ema deviation margin (negative = skip region).
    - RP: the 0-100 percentile rank of close within the current window.
    0.0 when the underlying state is unavailable (warmup / no ema).
    Cross-strategy normalization is intentionally deferred to the G2 audit (D).
    """
    del ld  # score derives from cached state, not the decision object
    if cell.strategy == StrategyName.MEAN_REVERSION:
        dev = strategy.last_deviation
        return float(dev) if dev is not None else 0.0
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        if candle.close is None or not strategy.window_filled:
            return 0.0
        window = strategy.window_values  # read-only snapshot, post-observe
        if not window:
            return 0.0
        close = candle.close
        return 100.0 * sum(1 for w in window if w <= close) / len(window)
    return 0.0
```

The reporter reads the strategy's public `window_values` snapshot (added in
Task 2) rather than the private `_window`, so there is no private-access lint and
the score derives from the same state both paths share.

- [ ] **Step 4: Run the score tests + full reporter module + mypy**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_divergence_reporter.py -q && uv run mypy src/`
Expected: PASS — score tests pass, byte-equivalence tests stay green (live and replay compute the same score), mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py backend_py/tests/modules/marketfeed/test_divergence_reporter.py
git commit -m "✨ Feat: continuous state-derived signal_score (G2 C)"
```

---

### Task 5: Full gate + finish

**Files:** none (verification only)

- [ ] **Step 1: Run the full non-integration suite + mypy + ruff**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: all green. If ruff flags the `strategy._window` private access or an unused import, fix inline and re-run.

- [ ] **Step 2: Confirm no behavior drift in strategy decisions**

Run: `cd backend_py && uv run pytest tests/modules/backtest tests/modules/marketfeed -q`
Expected: all green — proves `decide()`/`observe()` outputs unchanged (only observability added).

- [ ] **Step 3: Update the spec's status note**

In `docs/superpowers/specs/2026-05-31-g2-state-level-divergence-design.md`, no code change needed; this step is a placeholder reminder that component D remains deferred. No commit if nothing changed.

---

## Self-Review

- **Spec coverage:** A (props) → Tasks 1+2 ✓; B (richer `_strategy_attributes`, Decimal) → Task 3 ✓; C (continuous score) → Task 4 ✓; headline state-drift red test → Task 3 ✓; existing byte-equivalence stays green → Tasks 3/4 Step 4 + Task 5 ✓; out-of-scope D untouched ✓; safety invariant (no decide/observe behavior change) → Tasks 1/2 Step 5 + Task 5 Step 2 ✓.
- **Placeholder scan:** every code step shows full code; no TBD/TODO in implementation steps.
- **Type consistency:** property names `ema_current`/`last_deviation` (MR), `last_threshold`/`window_filled` (RP) are identical in Tasks 1/2 (definition), Task 3 (`_strategy_attributes` reads), and Task 4 (`_normalize_signal_score` reads). `signal_score` stays `float`. `_strategy_attributes` returns Decimal state values (not float-cast).
- **Known risk flagged:** RP `_window` private access in Task 4 — fallback (add `window_values` property) documented inline if mypy/ruff objects.

## Post-implementation (separate from this plan)

- Optional adversarial review of the diff (byte-equivalence preserved? no decide behavior change? Decimal precision held through the diff path?).
- Component D (G2 audit verdict script + M1/M2/M3 thresholds) remains deferred to shadow-window data (~2026-06-10).
- Then: merge + push (gh switch Will413028), update memory/strategy-journal.
