# Adaptive-Period Strategy — Design Spec

**Date**: 2026-06-03
**Status**: Approved (brainstorm) → ready for implementation plan
**Author**: Will + Claude (brainstorming session)
**Phase**: 3c (new strategy)

## 1. Context & Motivation

### 1.1 The fill-model insight (why period is the only untapped lever)

The backtest engine prices a lend decision against the market rate `candle.close`
via a linear fill model (`modules/backtest/config.py:46-60`):

```python
def compute_fill_prob(spread_pct, fill_alpha):  # fill_alpha = 5
    if spread_pct <= 0:
        return Decimal("1.0")
    return max(Decimal("0"), Decimal("1") - fill_alpha * spread_pct)
```

Expected-value analysis of posting `rate = market·(1+s)` for one decision:

- `s > 0` (above market): `EV ∝ (1+s)·(1−5s) = 1 − 4s − 5s²` → strictly **< 1**. Posting above market always loses (fill drop > rate gain).
- `s < 0` (below market): fill caps at 1.0, so `EV ∝ (1+s) < 1`. Also loses.
- `s = 0`: `EV ∝ 1`. **Optimal.**

**Conclusion: the optimal posting rate is exactly `candle.close`.** This is why every
existing strategy (`MeanReversion`, `RatePercentile`, `AlwaysMarketRate`) posts at
`candle.close`. Rate is pinned to market.

The only two alpha levers left are therefore:
1. **Gating** — when to lend vs pause (`MeanReversion`, `RatePercentile` use this).
2. **Period** — how many days to lock the rate (`LendDecision.period_days`).

**No existing strategy varies the period — `period_days=2` is emitted by all of them.**
This is the genuinely unexplored lever and the target of this strategy.

### 1.2 The economic logic of adaptive period

You always lend at the current market rate `candle.close` and lock it for `period`
days; during the lock you cannot re-price. Under a mean-reverting rate process
(`E[Δrate | level] ∝ −(level − mean)`):

- **Rate high vs its trend → expected to fall → lock LONG** (ride the locked-high rate down while market drops; you out-earn market for the lock duration).
- **Rate at/below trend → expected to rise → lock SHORT** (stay nimble, re-price upward frequently; don't get stuck at a low rate).
- **Spike (rate ≫ trend) → lock LONGEST** (capture the transient high before it reverts).

This is **the same mean-reversion thesis as `MeanReversion`, expressed through
duration instead of gating.** It reuses the proven deviation signal `(close − EMA)/EMA`
and the per-cell EDA `close_over_ema_sigma`.

> **Note — this contradicts the retired Go M5 spec**, which locked spikes *short* ("2d
> due to uncertain persistence"). For a *reverting* spike, locking long captures it; if
> the spike persists, long-vs-short is neutral. So spike → lock long is correct here.

### 1.3 Relation to the OOS findings (2026-06-02/03)

The recent OOS work established: **bot-vs-idle (market-rate capture) is the durable
value; MR timing alpha is a thin, regime-dependent bonus.** This strategy is
designed to honor that:

- It is **always-on at market rate** → captures full bot-vs-idle (never sacrifices the
  durable value to chase timing).
- Adaptive period is an *additive* attempt to beat the always-2d baseline — a bonus,
  not the headline. Characterization will report it the same way: primary =
  bot-vs-idle, secondary = vs `AlwaysMarketRate` (period=2).

## 2. Goal & Non-Goals

**Goal**: A new `AdaptivePeriodStrategy` that lends at `candle.close` every cycle and
selects `period_days` from the rate's deviation-from-EMA, fully unit-tested and
backtest-characterized over full history + recent window — **with zero impact on the
live fUST/fUSD MeanReversion canary.**

**In scope (v1)**:
- `AdaptivePeriodStrategy` class implementing the `Strategy` ABC.
- `StrategyName.ADAPTIVE_PERIOD` enum + `AdaptivePeriodParams` validator + `build_strategy` dispatch.
- `param_grid_for_cell` for future sweeps.
- Full unit-test suite (TDD).
- OOS characterization via the existing harness, using a **dedicated experimental
  cells file** (`configs/cells.experimental.yaml`), driven by a small `--cells` arg
  added to `run_oos_profitability.py`. **`cells.canary.yaml` is NOT touched.**

**Out of scope (deferred to a separate deploy-time follow-up, gated by live G3)**:
- Wiring into `derive_cells --write/--check` (the automated drift gate is MR-specific;
  see §6.2).
- Adding `adaptive_period` cells to `cells.canary.yaml` / live deployment.
- `divergence_reporter` period-boundary handling (live-only; designed in §7, implemented at live-wiring time).

**Non-goals (YAGNI)**:
- Above-market pricing (proven net-negative under the fill model, §1.1).
- A separate spike-detector module (spike is subsumed as the top period tier, §3).
- Composition/overlay layer (architecture is one-strategy-per-cell; keep it).
- Momentum/trend (short-vs-long EMA) signal (level/deviation chosen; §3).

## 3. The Mechanism

**Signal** (identical to `MeanReversion`):
```
deviation = (candle.close - EMA) / EMA          # EMA via exponential smoothing, alpha = 2/(ema_span+1)
band1 = T1 * ratio_sigma                          # T1, T2 in sigma units (like MR threshold_sigma)
band2 = T2 * ratio_sigma                          # ratio_sigma = EDA close_over_ema_sigma_{span}
```

**Mapping** (tiered, deterministic):

| Condition | `period_days` | Rationale |
|---|---|---|
| not yet warmed (EMA `None` or `samples < ema_span`) | `P_FLOOR` (2) | unreliable deviation → safest shortest lock |
| `deviation ≤ band1` | `P_FLOOR` (2) | rate ≤ trend → re-price up frequently |
| `band1 < deviation ≤ band2` | `P_MID` (7) | mildly above trend → medium lock |
| `deviation > band2` | `P_LONG` (30) | well above trend / spike → lock long, ride reversion |

- **Always-on, never pauses** — `decide()` always returns a `LendDecision` with
  `rate = candle.close` (pause is MeanReversion's job; this strategy maximizes
  bot-vs-idle). If `candle.close is None` (data gap) → return `None` (same as all strategies).
- **Spike subsumed** — no separate branch; `deviation > band2` is the spike tier.
- **Clamp** — final `period_days` clamped to `[2, 120]` (Bitfinex funding offer bounds).
  `P_FLOOR/P_MID/P_LONG` are inside this range by construction; the clamp is a hard invariant guard.

**State held** (mirrors MeanReversion): `_ema`, `_alpha` (cached), `_samples` (count for warmup gate). Expose via `@property` getters (`ema_current`, `last_period`, `samples`, `window_filled`) for test inspection and divergence comparison.

## 4. Interface Integration

A new strategy slots into the shared backtest+live path (`build_strategy` is used by
both `oos_eval` and live `signal_engine`) with **no harness changes**. Required pieces:

1. **`AdaptivePeriodStrategy(Strategy)`** — `modules/backtest/strategies/adaptive_period.py`
   - `name` property → `"adaptive_period_p{...}"`-style string (mirror MR's name format; verify name string is not persisted to DB — MR's isn't).
   - `observe(candle)` → update `_ema`, `_samples` (skip if `close is None`).
   - `decide(candle)` → the §3 mapping, returns `LendDecision(mts, rate=close, period_days=mapped)` or `None` if `close is None`.
   - `param_grid_for_cell(symbol, period_agg, eda)` → §5.
2. **`StrategyName.ADAPTIVE_PERIOD = "adaptive_period"`** — add to the enum.
3. **`AdaptivePeriodParams`** validator (mirror `MeanReversionParams`) — fields:
   `ema_span: int`, `ratio_sigma: Decimal`, `t1: Decimal`, `t2: Decimal`,
   `p_mid: int`, `p_long: int` (with `p_floor` fixed at 2). Validate `t2 > t1 ≥ 0`,
   `2 ≤ p_mid ≤ p_long ≤ 120`. Wire into `CellConfig` params validation.
4. **`build_strategy` dispatch** (`strategy_registry.py:41-56`) — add an `ADAPTIVE_PERIOD` branch extracting the params dict.

**`decide` contract** is unchanged: returns `LendDecision(mts, rate: Decimal, period_days: int) | None`. Live plumbing for variable period is **confirmed end-to-end**:
`LendDecision.period_days` → `signal_engine.py:267` (`offer_duration_days`) →
`StandingQuote.period_days` (`standing_quote.py:21`) → `reconciler.py:219` →
`live_executor.py:89` Bitfinex submit body `"period"`.

## 5. param_grid_for_cell & Sweep

Mirror `MeanReversion.param_grid_for_cell`: pull `ratio_sigma` per span from EDA
(`close_over_ema_sigma_24/168`), then enumerate a small grid:

- `ema_span ∈ {24, 168}` (with the matching `ratio_sigma` per span)
- threshold pairs `(T1, T2) ∈ {(0.5, 1.5), (1.0, 2.0)}`
- period tiers **fixed for v1**: `P_MID=7`, `P_LONG=30` (sweeping tiers too explodes
  the grid and overfit risk; revisit only if characterization motivates it)

→ ~4 variants/cell (× `ema_span` already gives 2×2 = 4). Same order of magnitude as
MR's 6; keeps the deflated-Sharpe `n_trials` honest.

For v1 **manual characterization**, pick one representative config per cell (e.g.
`ema_span=24, T1=0.5, T2=1.5, P_MID=7, P_LONG=30`, `ratio_sigma` borrowed from the
matching canary cell — it is strategy-independent EDA). The full WFO sweep + `select_winner`
is a `derive_cells` concern, deferred (§6.2).

## 6. Backtest Characterization

### 6.1 Harness reuse (no changes to engine/oos_eval)

The strategy auto-runs in `evaluate_oos_windows` / `run_oos_profitability` because it
implements the `Strategy` ABC. The engine already honors variable `period_days`
(compounds `(1 + rate·period)`, cooldown = `period·24h + gap`). Characterization mirrors
the MR reports:

- **Primary**: bot-vs-idle monthly return (the strategy's absolute return; idle = 0%).
- **Secondary**: active return vs `AlwaysMarketRate(period_days=2)` — this isolates the
  **period alpha**: does adaptive period beat always-2d at the same market rate?
- Run over **full history** (fUST 2018→, fUSD 2016→) and **recent window** (2022→),
  the two regimes we already characterized for MR, for apples-to-apples comparison.
- Report: bootstrap CIs + deflated-Sharpe, same as `run_oos_profitability.py`.

### 6.2 Config plumbing (keep the MR drift gate untouched)

`run_oos_profitability.py` hardcodes `CANARY_YAML`. Add an optional `--cells PATH` arg
(default = `configs/cells.canary.yaml`). Create `configs/cells.experimental.yaml` with
`adaptive_period` cells for fUST/fUSD × {a30, p2}. Run characterization against that file.

**Do NOT add `adaptive_period` cells to `cells.canary.yaml`.** `check_against_fixture`
(`cell_pipeline.py:116-189`) re-derives only `MR_CELLS` and asserts canary↔main parity —
it is MeanReversion-specific. Wiring `adaptive_period` into `derive_cells --write/--check`
is deferred deploy-time work; v1 characterization needs none of it.

## 7. Live Deployment Path (deferred, documented now)

When (and only when) characterization is favorable AND live G3 has cleared, a separate
follow-up wires deployment:

1. Extend `derive_cells` to sweep+select `adaptive_period` params (or hand-set them and
   exclude from the MR drift gate).
2. Add cells to `cells.canary.yaml` behind the per-currency cap/balance gates.
3. **divergence_reporter period-boundary handling** — `period_days` is a step function of
   `deviation`, which depends on `_ema` (compared with `rel_tol=1e-4`; ints compared
   exactly per `divergence_reporter.py:174-184`). Near a tier boundary, tiny live/replay
   EMA drift can flip `period` (2↔7↔30) → false divergence (same class as
   [[g2-state-level-divergence]]). **Design**: when the underlying `deviation` is within
   `rel_tol` of `band1`/`band2`, treat a `period` mismatch as tolerated (not a hard
   divergence); or apply hysteresis to tier transitions. Implement at live-wiring time
   (divergence_reporter does not run in backtest-only v1).

## 8. Edge Cases & Invariants

- `candle.close is None` → `decide` returns `None` (data gap; matches all strategies).
- Warmup (`_ema is None` or `_samples < ema_span`) → `period_days = 2`.
- `EMA = 0` guard → avoid div-by-zero in deviation (shouldn't happen for positive rates, but guard).
- **Invariant**: `decide()` (when it returns a decision) always returns `rate == candle.close` and `2 ≤ period_days ≤ 120`. Assert in tests.
- Determinism: same warmed state + same candle → same `period_days` (required for live==replay).

## 9. Testing Plan (TDD)

File: `tests/modules/backtest/strategies/test_adaptive_period.py`, following the
existing convention (local `_c(mts, close)` builder, deterministic golden values,
property getters). Write tests first.

Cases:
1. **Warmup** → `period_days == 2` before `ema_span` samples seen.
2. **At/below trend** (`deviation ≤ band1`) → `period_days == 2`.
3. **Mildly above trend** (`band1 < deviation ≤ band2`) → `period_days == P_MID` (7).
4. **Spike** (`deviation > band2`) → `period_days == P_LONG` (30).
5. **Boundary** values (`deviation == band1`, `== band2`) → exact tier assignment (pin the `≤` semantics).
6. **rate == candle.close** always (the fill-model invariant).
7. **`close is None`** → `decide` returns `None`.
8. **period clamp** to `[2, 120]` (construct params at the edge; assert clamp).
9. **`name` property** returns expected string.
10. **`param_grid_for_cell`** returns the expected variant set from a seeded `eda` dict.
11. **Determinism** — two instances fed the same candle sequence produce identical decisions.
12. **registry** — `build_strategy` on an `adaptive_period` `CellConfig` returns a correctly-parameterized instance.

Gate: `cd backend_py && uv run pytest -m "not integration"` all green; `mypy src/` clean; `ruff check` clean.

## 10. Risks & Non-Backtestable Caveats

- **Momentum failure mode**: locking long at a high that keeps rising (momentum, not
  reversion) → stuck below market. Mitigated by mean-reversion prior + `P_LONG` cap.
  Backtest quantifies the cost.
- **Backtest optimism**: fill model = 100% at market; live partial/slow fills mean
  realized ≤ backtest (same caveat as all our OOS work). Longer locks also mean fewer
  re-pricing opportunities — a real difference vs always-2d that the model captures only
  under its mean-reversion-in-data assumption.
- **Platform/credit tail** (Bitfinex/Tether) — unmitigated by any backtest; bounded by
  position caps, not this strategy. Longer locks marginally increase tail exposure
  duration (you're committed for up to 30 days) — note but accept (cap-bounded).
- **Overfit**: tiers fixed for v1, small grid, deflated-Sharpe reported — keep `n_trials`
  honest.

## 11. Deferred / Open Items

- `derive_cells` sweep+select integration for `adaptive_period` (deploy-time).
- `divergence_reporter` period-boundary handling (live-wiring time, §7).
- Live deployment behind per-currency caps (gated by live G3 verdict + favorable characterization).
- Possible v2: sweep period tiers; hybrid level+trend; per-cell `P_LONG` from data.

## 12. Key File References

- Strategy ABC: `modules/backtest/strategies/base.py:8-46`
- MeanReversion (template): `modules/backtest/strategies/mean_reversion.py:11-84`
- AlwaysMarketRate (baseline): `modules/backtest/strategies/always_market_rate.py`
- FundingCandle: `modules/candles/schemas.py:14-66`
- LendDecision: `modules/backtest/schemas.py:32-44`
- Fill model: `modules/backtest/config.py:46-60`; engine: `modules/backtest/engine.py:26-164`
- build_strategy / StrategyName: `modules/marketfeed/strategy_registry.py:41-56`
- CellConfig / Params: `modules/marketfeed/config.py:50-84`
- OOS harness: `modules/backtest/oos_eval.py:32-73`; `scripts/run_oos_profitability.py`
- Drift gate (MR-specific): `modules/backtest/cell_pipeline.py:116-189`; `scripts/derive_cells.py`
- Live period plumbing: `signal_engine.py:180,267` → `standing_quote.py:21` → `reconciler.py:219` → `live_executor.py:89,217`
- divergence_reporter: `modules/marketfeed/divergence_reporter.py:119-184`
