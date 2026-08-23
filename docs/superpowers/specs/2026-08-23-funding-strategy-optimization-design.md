# Funding Strategy Rate Optimization Design

**Date:** 2026-08-23  
**Status:** Approved for implementation  
**Scope:** repository changes only; no remote deployment and no live canary parameter changes

## Goal

Improve expected net lending return without sacrificing safety by making backtest fill assumptions explicit, making live book pricing period-aware, and collecting a shadow-only quote recommendation before any submit-rate change is armed.

## Current problem

The active Python backend currently has three gaps:

1. `run_backtest()` supports an injected `FillRateModel`, but the WFO matrix and fixed OOS helpers do not pass one, so the matrix silently uses the linear fallback.
2. `book_clamp.py` consumes scalar ticker best bid/ask values. Bitfinex funding-book levels include `rate`, `period`, `count`, and `amount`; a global best ask can belong to a different funding duration than the quote being evaluated.
3. The strategy signal and execution clamp have no expected-value comparison between the signal rate and a period-compatible book price. Adding a fixed premium would optimize nominal rate while ignoring fill probability, so the first optimizer will only emit an observation and will not alter submits.

The current canary is already resumed on the remote VM, but the rollout gate remains operational: this change must not modify `cells.canary.yaml`, safety caps, the persisted halt, or the remote deployment.

## Design

### 1. Empirical fill model reaches WFO and OOS

Thread an optional `BacktestConfig` and `FillRateModel` through:

- `run_cell_wfo()` for baseline, train, and test runs;
- `evaluate_oos_windows()` for strategy and baseline runs;
- `derive_cell_params()` for parameter derivation.

Existing callers retain deterministic linear behavior unless they explicitly request `BacktestConfig(fill_model="empirical")` and provide a model. The phase-3b matrix script gains an explicit `--fill-model linear|empirical` option. In empirical mode it loads `source="candle"` stats per symbol from `fill_rate_stats`; a model loaded for one symbol is never reused for another symbol.

Low-confidence or missing buckets continue to fall back per lookup to `compute_fill_prob()`, preserving the existing model contract. The output must identify which fill model was used so a report cannot be mistaken for a linear run.

### 2. Period-aware book clamp

Extend the pure clamp policy with an opt-in `period_aware` flag and extend `clamp_rate()` with:

- the target `quote_period_days`;
- optional `FundingBookLevel` rows.

When `period_aware` is false, the current ticker behavior remains byte-compatible. When true:

- maker competition is derived only from ask levels whose `period` equals `quote_period_days`;
- taker selection requires an exact-period bid with sufficient absolute amount;
- absent or insufficient exact-period book data returns the original quote through a distinct fallback branch;
- the existing maximum-down floor remains in force;
- the clamp never silently changes the requested duration.

`DeploymentReconciler` receives an optional book source and fetches one book snapshot per symbol per reconcile tick only when the opt-in flag is enabled. Fetch errors are fail-open to the original quote. The existing ticker remains available for legacy clamp behavior and reprice telemetry.

New environment behavior:

- `BFX_CLAMP_PERIOD_AWARE_ENABLED` defaults to `false`;
- `BFX_CLAMP_ENABLED` remains the independent enforcement switch;
- no canary environment file is changed to enable either new behavior.

### 3. Shadow-only quote optimizer

Add a pure `rate_optimizer` module. It evaluates a small, deterministic candidate set for one quote:

- the strategy signal rate;
- the best exact-period maker price (`best_ask - TICK`) when available;
- the best exact-period ask when available.

Candidates below the signal rate are excluded because the signal rate remains the strategy floor. For each remaining candidate, score:

```text
expected_net_daily_rate = candidate_rate × fill_probability × (1 - fee_rate)
```

`fill_probability` comes from the empirical model for the cell's `period_agg` and horizon. If no high-confidence estimate is available, the optimizer returns no recommendation. Ties prefer the higher fill probability, then the lower quote rate.

The live reconciler may receive per-symbol fill models and an observation policy. With `BFX_RATE_OPTIMIZER_OBSERVE=true`, it logs the selected candidate and the current quote; it still submits the existing clamped quote. The default is false, and there is no production path that consumes the optimizer's selected rate in this change.

### 4. AdaptivePeriod p14 shadow profile

`configs/cells.experimental-p14.yaml` already contains the locked `AdaptivePeriodStrategy` parameters (`p_mid=7`, `p_long=14`, `t1=0.5`, `t2=1.5`). Add an explicit `shadow-p14` deployment profile that selects this file while keeping `BFX_PHASE=shadow` and `BFX_DEPLOYMENT_ENV=shadow`. The profile is opt-in and is not a canary configuration.

The profile must be accepted by `scripts/deploy-vm.sh` without relaxing the canary confirmation gate. No production cap, cell, or period is changed.

## Data flow

```text
fill_rate_stats ──► per-symbol FillRateModel ──► WFO/OOS scoring
                                              └─► optional live observer

signal rate + exact-period book ──► period-aware clamp ──► existing submit rate
                                └─► shadow optimizer ──► log only

shadow-p14 env ──► AdaptivePeriod p14 ──► simulated decisions / parity evidence
```

## Safety and rollout

- No remote command, container restart, deploy, cap change, rate change, or resume call is part of this implementation.
- New runtime flags default to disabled.
- Existing `cells.canary.yaml` and `safety.canary.yaml` remain unchanged.
- Book fetch failure, missing fill stats, low-confidence fill stats, and optimizer exceptions must not block or change normal reconciliation.
- The optimizer is not allowed to submit its selected rate in this scope.
- A future promotion requires shadow evidence, L3 stability, corrected two-series L4 review, and an explicit operator decision.

## Verification

Unit tests must cover:

- empirical model propagation into WFO train/test/baseline;
- per-symbol fill-model isolation;
- exact-period book selection, insufficient depth, missing period, and legacy fallback;
- optimizer candidate filtering, expected-net-rate selection, low-confidence no-op, and fee application;
- reconciler observe-only behavior proving the submitted `DecisionPayload.offer_rate` remains unchanged;
- `shadow-p14` config selection and canary profile exclusion.

Run the targeted tests first, then `cd backend_py && uv run pytest -m "not integration"`, `uv run mypy src/`, and `uv run ruff check` before claiming completion.

## Non-goals

- No FRR floor promotion.
- No fixed weekend or spike premium.
- No automatic cap increase.
- No live activation of AdaptivePeriod.
- No historical book reconstruction beyond the existing self-recorded snapshots.
