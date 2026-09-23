# G2 — State-level divergence detection for live-vs-replay parity

Date: 2026-05-31
Scope: the A+B+C "detection capability" slice of the G2 calibration work.
Component D (audit verdict script + M1/M2/M3 thresholds) is OUT OF SCOPE —
deferred until the shadow window (~2026-06-10) yields a real divergence
distribution to set data-driven thresholds.

## Problem

`modules/marketfeed/divergence_reporter.py` is the live-vs-replay signal
divergence detector for the Phase 4.1 shadow infra. It compares a `live_signal`
(from the daemon's long-lived, incrementally-`observe()`d strategy in the
StrategyRegistry) against a `replay_signal` (rebuilt from scratch each tick via
`build_strategy_at_boundary`). The two code paths are genuinely independent:

- **live**: `warmup_cell` builds the strategy once at daemon start, then every
  boundary candle is fed via `signal_engine → ExtractedSignal.extract → observe`
  — a long-running incremental accumulation (days/weeks).
- **replay**: the reporter rebuilds from scratch each tick.

The current comparison only sees `signal_direction` (POST/SKIP) plus *static*
attributes (config params + `candle.close`). It does **not** expose the
strategies' decision-determining internal accumulators:

- `MeanReversionStrategy._ema` — an incrementally-updated EMA accumulator (the
  most drift-prone state: `ema = α·close + (1-α)·ema` applied N times live vs
  rebuilt in a batch on replay).
- `RatePercentileStrategy._window` — the rolling deque whose `np.percentile`
  yields the lend threshold.

**Consequence (the bug class this closes):** if live and replay accumulators
silently drift (a LOCF off-by-one, an observe-ordering difference) but the
resulting action still lands on the same side of the threshold, the current
reporter reports **no divergence**. The latent drift then erupts as a wrong
action on some later boundary candle — exactly when real money is at stake.
This is the classic online-offline parity gap (quant backtest-live parity /
ML training-serving skew): comparing outputs, not the features/state behind them.

## Approach — lift detection from action-parity to state-parity

Best practice for online-offline parity has three principles; this slice
implements the first two (the third belongs to deferred component D):

1. **Compare at state level, not just action.** Expose the decision-determining
   derived state and include it in the divergence diff. Any field drift flags.
2. **Score is a continuous function of state, not a binary.** Replace the
   `±1.0` placeholder with a strategy-specific signal-strength quantity so the
   eventual audit can reason about distribution distance, not just binary match.
3. *(Deferred to D)* **Thresholds are data-driven** — collect the shadow
   window's divergence distribution, then set M1/M2/M3.

**Invariant (safety):** this change adds *observability only*. It does NOT alter
any strategy's `decide()` behavior, `observe()` math, or `name`. Backtest results
and live signal directions are byte-for-byte unchanged. (Same discipline as G3
Stage 2 not touching the verdict table.)

## Components

### A. Expose decision-determining derived state as read-only `@property`

Minimal-intrusion: cache the value `decide()` already computes; don't change the
`decide()` signature or touch the backtest core.

**`MeanReversionStrategy`** (`modules/backtest/strategies/mean_reversion.py`):
- `ema_current: Decimal | None` → returns `self._ema` (the live accumulator).
- `last_deviation: Decimal | None` → `(close - ema)/ema` from the most recent
  `decide()`; cache as `self._last_deviation` inside `decide()` (None until the
  first `decide()` with a usable ema).

**`RatePercentileStrategy`** (`modules/backtest/strategies/rate_percentile.py`):
- `last_threshold: Decimal | None` → the `np.percentile` threshold from the most
  recent `decide()`; cache as `self._last_threshold` inside `decide()` (None
  while the window is still warming up — i.e. `decide()` returned early).
- `window_filled: bool` → `len(self._window) >= self._lookback_hours`.

These are pure read-accessors over existing state; the cache assignments are the
only `decide()` edits and do not change its return value.

### B′. Bounded vs unbounded state — comparison semantics (added post-review)

An adversarial review surfaced that **byte-equality is the right invariant only
for BOUNDED state, not unbounded accumulators.** RP's window is a `maxlen` deque
that forgets old data exactly, so live (warmup-seeded, incrementally observed) and
replay (rebuilt from a lookback window each tick) are byte-equal. MR's `_ema` is an
**unbounded accumulator**: live is seeded once at warmup and drifts forward; replay
re-seeds at `ref_mts − lookback` every tick. The seed's exponential tail
(`(1−α)^lookback ≈ 5.6e-8` for span=24, lookback=200) is benign convergence noise
but **never reaches 0 in Decimal** — so exact comparison of `ema_current` would
flag MR divergence on essentially every steady-state tick (the deployed canary is
MR span=24).

Fix (standard online/offline parity practice): compare accumulator-derived fields
(`ema_current`, `last_deviation`, MR `signal_score`) with a **relative tolerance
`1e-4`** (≈140× above the span=24 noise floor, ≈100× below real-drift magnitude
~1e-2); keep bounded/discrete fields (RP state, config, `signal_direction`) exact.
`_diff_fields` drives the divergence decision (not dataclass `==`). Quantization
was rejected — a rounding grid has a boundary artifact (values ~1e-8 apart
straddling an edge falsely diverge). **Deferred:** span=168 cells do not converge
within lookback=200 (~9% floor); they need a larger lookback to be
tolerance-comparable — not deployed.

### B. Include derived state in `_strategy_attributes` (the divergence vector)

`divergence_reporter._strategy_attributes` adds the new state fields per strategy,
**keeping Decimal precision** (do NOT cast to float — a float cast would mask
sub-Decimal drift, defeating the purpose). `ExtractedSignal.strategy_attributes`
is already a `tuple[tuple[str, Any], ...]` of sorted items and Decimal is
hashable, so the frozen-dataclass equality / byte-equivalence machinery is
unchanged. `_diff_fields` already diffs `strategy_attributes` wholesale, so any
new field's drift is flagged automatically.

- RP: `{percentile, last_close, last_threshold, window_filled}`
- MR: `{rate, threshold_sigma, ema_current, last_deviation}`

(`last_threshold` / `last_deviation` / `ema_current` may be `None` during warmup
— `None == None` is fine; both paths warm up identically by construction.)

### C. `_normalize_signal_score` = continuous signal-strength function of state

Replace the direction-only `±1.0` with a strategy-specific continuous quantity
(same-strategy live-vs-replay must still be exactly equal):

- MR: `float(last_deviation)` — the continuous `(close-ema)/ema` margin
  (negative = below EMA, the skip region). `0.0` when state unavailable.
- RP: `percentile_rank` — the rank (0–100) of `candle.close` within the current
  window (`scipy`/`numpy`-free: `100 * count(w <= close) / len(window)`); `0.0`
  while the window is empty.

These are strategy-specific "signal strength" quantities — intentionally NOT
cross-strategy-normalized. Cross-strategy normalization is only needed for D's
audit aggregation and is deferred (YAGNI). `signal_score` stays `float` (a
human-readable continuous diagnostic); exact drift detection is carried by the
Decimal fields in `strategy_attributes`, not by the float score.

## Data flow (unchanged shape, richer payload)

```
boundary candle ─┬─ live: registry strategy (warmup + incremental observe) ─┐
                 │                                                          ├─ ExtractedSignal
                 └─ replay: build_strategy_at_boundary (rebuild) ───────────┘   { direction,
                                                                                  strategy_attributes:  ← now incl. ema/threshold/…
                                                                                  signal_score }        ← now continuous
                 DivergenceReporter.check → _diff_fields → divergence dict (Axiom event, unchanged sink)
```

## Testing (TDD)

New tests:
- **State-drift-but-same-direction detection (the headline red test):** construct
  a `live_signal` whose `signal_direction` MATCHES replay but whose `ema_current`
  (MR) / `last_threshold` (RP) differs → assert the reporter now returns a
  divergence dict with `strategy_attributes` in `diff_fields`. This is the bug
  the old direction-only reporter missed; it must fail before B and pass after.
- **Property accessors:** `ema_current` reflects `_ema` after observes;
  `last_deviation` / `last_threshold` cache the most recent `decide()` value and
  are `None` during warmup; `window_filled` flips at `lookback_hours`.
- **Score formula:** MR `signal_score == float(deviation)`; RP `signal_score ==`
  expected percentile rank for a known window; both `0.0` when state unavailable.

Existing tests that MUST stay green (no behavior change):
- `test_divergence_reporter.py`: `test_no_divergence_when_inputs_match`,
  `test_replay_byte_equivalent_with_locf_on_sparse_input`,
  `test_cp1_byte_equivalence_property` (hypothesis), and the hard-trigger
  detection test.
- The backtest strategy suites (decide/observe behavior unchanged).

Gate: `cd backend_py && uv run pytest -m "not integration"` + `uv run mypy src/`
+ `uv run ruff check` all green before commit.

## Out of scope (deferred / explicitly not this PR)

- **Component D**: G2 audit verdict script (a `run_g2_calibration_audit`
  analogue), the M1 signal-match-rate / M2 param-drift / M3 infra-gap formulas,
  and their threshold numbers — all data-driven from the shadow window (~6/10).
- **Cross-strategy score normalization** (only D's aggregation needs it).
- **Axiom querying** of `signal_divergence` events (D).
- Any change to `decide()`/`observe()` decision behavior, strategy `name`, or the
  backtest engine.

## Risks / verify adversarially (post-implementation)

- **RP float path in byte-equivalence:** `last_threshold` flows through
  `np.percentile` (float) then `Decimal(str(float(...)))`. Confirm live and replay
  both take the identical float path so the Decimal'd threshold is byte-equal
  (it is, since both call the same `decide()` — but pin it with a test).
- **Cache timing:** `last_deviation`/`last_threshold` reflect the most recent
  `decide()`. `ExtractedSignal.extract` calls `observe` then `decide`, so the
  property reflects the boundary candle's state. Verify no path reads the
  property before a `decide()`.
- **None-equality during warmup:** both paths warm up identically, so
  `None == None` holds; confirm a warmup-phase boundary still yields no false
  divergence.
