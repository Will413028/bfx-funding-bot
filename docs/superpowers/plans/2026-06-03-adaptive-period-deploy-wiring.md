# Adaptive-Period Deploy Wiring (B1 + B2 now; B3 gated)

> Follow-up to `2026-06-03-adaptive-period-strategy.md`. Deploy `p_long=14` (sweep `d761cd7`). B1+B2 build now (repo-only, no live impact — adaptive_period is not in canary). B3 is a runbook only; live arming is gated on **Gate A** (live MR G3 verdict ≥8 weekly windows) + **Gate B** (adaptive_period shadow/parity) + human sign-off + manual `deploy-vm.sh`.

## B1 — divergence_reporter adaptive_period branch

**Problem (final-review blind spot):** `_strategy_attributes` and `_normalize_signal_score` in `backend_py/src/bfx_funding_bot/modules/marketfeed/divergence_reporter.py` return `{}` / `0.0` for adaptive_period → if it ran live, live==replay parity would only compare `signal_direction`, missing genuine EMA-state drift.

**Design (reframed — simpler & more correct than "tolerance-compare the period"):** `period_days` is a *deterministic step function* of `_period_for(ema, close, config)`. If `ema_current` matches within rel-tol AND config + boundary candle are identical, the period is identical by construction. Therefore:
- **Do NOT put the derived `period_days` in the compared attributes** — comparing a step function of a tolerance-compared input only manufactures false divergences at tier boundaries (band1/band2).
- **Compare its continuous/discrete *inputs* instead:** `ema_current` (tolerance-compared — already in `_APPROX_ATTR_KEYS`), `window_filled` (exact — warmup parity), and the config params `t1`/`t2`/`ratio_sigma` (exact — config drift). This is **necessary and sufficient**: genuine drift surfaces via `ema_current`; boundary flips with matching ema are correctly ignored.

**Changes (one file + tests):**
1. `_strategy_attributes` — add, after the `MEAN_REVERSION` branch:
   ```python
   if cell.strategy == StrategyName.ADAPTIVE_PERIOD:
       # period_days is a deterministic step fn of (ema, close, config); comparing
       # the derived period would only manufacture false divergences at the band
       # tier boundaries. We compare its INPUTS instead: ema_current (rel-tol via
       # _APPROX_ATTR_KEYS) catches genuine drift; window_filled (exact) catches
       # warmup parity; t1/t2/ratio_sigma (exact) catch config drift. Sufficient.
       return {
           "rate": float(candle.close) if candle.close is not None else 0.0,
           "t1": float(cell.params["t1"]),
           "t2": float(cell.params["t2"]),
           "ratio_sigma": float(cell.params["ratio_sigma"]),
           "ema_current": strategy.ema_current,
           "window_filled": strategy.window_filled,
       }
   ```
2. `_normalize_signal_score` — add, before the final `return 0.0`:
   ```python
   if cell.strategy == StrategyName.ADAPTIVE_PERIOD:
       ema = strategy.ema_current
       if ema is None or ema == 0 or candle.close is None:
           return 0.0
       return float((candle.close - ema) / ema)  # deviation, mirrors MR
   ```
   (`ema_current` already in `_APPROX_ATTR_KEYS`; `_REL_TOL` unchanged. No schema change — `ExtractedSignal` already carries everything.)

**Tests** (`tests/modules/marketfeed/test_divergence_reporter.py`, mirror the MR ones):
- `test_adaptive_period_state_drift_detected`: live signal carries a drifted `ema_current` vs replay → `strategy_attributes` in `diff_fields` (mirror `test_state_drift_detected_even_when_direction_matches`).
- `test_adaptive_period_no_false_divergence_on_boundary_period_flip`: construct live vs replay where `ema_current` matches within rel-tol but the *period would differ across a tier boundary* — assert `reporter.check(...)` returns `None` (NO divergence), proving the reframed design. (Build the live signal with ema within `_REL_TOL` of the replay's; since period isn't in attrs, no flag.)
- `test_adaptive_period_warmup_no_false_divergence`: mirror `test_mr_warmup_drift_no_false_divergence` (EMA convergence within tolerance over warmup).
- `test_adaptive_period_config_drift_detected`: a t1/t2/ratio_sigma mismatch → divergence.

## B2 — adaptive_period deploy gate (non-inert guard for p14)

**Context:** the MR drift gate `cell_pipeline.check_against_fixture` filters to `mean_reversion` cells only (`_mr_cells_in`) and silently ignores adaptive_period — so adding an adaptive_period cell later does NOT break it (no change needed there). What IS needed is the analog of the MR deploy gate (`test_deploy_gate_e2e` / `pytest -m gate`): a permanent CI guard that the **p14 deploy-candidate params beat the passive AlwaysMarketRate baseline** (non-inert), so a future param edit can't ship a dud.

**Change:** add a gate test (match the existing gate-test marker/convention found in `test_deploy_gate_e2e.py` + `modules/backtest/deploy_gate.py`) that:
- loads the p14 deploy-candidate cells from `configs/cells.experimental-p14.yaml` via `load_cells_only`,
- for each, builds the strategy via the generic `build_strategy(cell)` factory (NOT hardcoded MeanReversion),
- runs the existing deploy-gate check (the one MR uses) against the `AlwaysMarketRate` passive baseline,
- asserts each cell passes (period alpha / beats-passive > 0).

Investigate `deploy_gate.py` + `test_deploy_gate_e2e.py::_gate_for` first to reuse the exact gate primitive and baseline. If the gate primitive is generic (takes a `make_strategy`), this is a small addition; if it hardcodes MR, refactor it to take `build_strategy(cell)` (behavior-preserving for existing MR cells — verify they still pass).

## B3 — canary cell (RUNBOOK ONLY, gated — do NOT apply now)

When **Gate A** (live MR G3 verdict ≥8 weekly windows, favorable) AND **Gate B** (adaptive_period shadow/parity) clear, with human sign-off:

1. Add ONE adaptive_period cell to `backend_py/configs/cells.canary.yaml` (p14 params), behind the per-currency caps/balance gate. Start DARK or shadow (e.g. tiny cap), then small real money. Exact cell:
   ```yaml
     - strategy: adaptive_period
       symbol: fUST
       period_agg: a30
       timeframe: 1h
       params: {ema_span: 24, ratio_sigma: 0.42049266874194213, t1: 0.5, t2: 1.5, p_mid: 7, p_long: 14}
   ```
2. **Tests that MUST be updated when the cell is added** (they currently assume canary is all-MR / count 4):
   - `tests/scripts/test_run_oos_profitability.py::test_canary_yaml_loads_cells` — count 4→5 and drop/loosen `all(strategy == mean_reversion)`.
   - `tests/modules/backtest/test_deploy_gate_e2e.py` — its canary-cell parametrization must dispatch via `build_strategy` (done in B2 if refactored) so the adaptive cell is gated correctly, not built as MR.
   - Re-check any other test grepping canary cell count/strategy.
3. Per-currency caps: ensure `caps`/balance-gate cover the new cell's symbol (fUST already capped at 3000); decide the adaptive cell's allocation share.
4. Deploy via the manual VM flow (push origin/main with `gh auth switch -u Will413028` → `deploy-vm.sh canary` with `BFX_CANARY_CONFIRM`); verify byte-identical fUST realized + the new cell's behavior; KILL_SWITCH / revert plan ready.

**Do not commit B3 to `cells.canary.yaml` now** — it would change the live config the VM deploys from and break the count-tests. B3 is documented here for the gated future.
