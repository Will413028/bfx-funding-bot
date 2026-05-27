# Canary OOS Profitability Validation — Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-28
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-28
**Closes**: backlog #4「策略獲利量化驗證」— reframed (see Why)

## Why

Backlog #4 was originally framed as an absolute ship gate: `月化 ≥1.5% AND drawdown <15%`.
Two findings invalidate that framing:

1. **`drawdown <15%` is a category error for a yield/carry strategy.** The backtest
   equity update is `equity *= (1 + net_rate * period)` with `net_rate ≥ 0` always
   (lending earns interest, never loses principal in-model). Equity is monotonically
   non-decreasing → `max_drawdown_pct` is structurally `0` in every backtest
   (`engine.py:121-133`). Max-drawdown is a directional-trading risk metric; it does
   not transfer to lending.
2. **The `1.5%` floor has no provenance.** It appears in no spec, ROADMAP, or
   `strategy_specification.md` — it was a prior session's invented number, not anchored
   to the passive baseline's actual return nor to Will's decision.

What we actually have: Phase 3b WFO + Phase 4.3 LOCF proved the shipped strategy beats
the AlwaysFRR baseline **relatively** (win%, margin, health). What we do **not** have:
the **absolute** OOS return distribution of the deployed config, nor a risk frame that
fits a yield strategy. This validation fills that gap.

The strategy class is **yield / carry** (lend at funding rate, earn interest), not
directional. The industry validates carry strategies on: realized net yield, **active
return vs the passive benchmark** (= always-lend-at-FRR), **utilization / idle rate**
(the dominant downside — selective lenders sit out and earn 0), and **tail / worst-period**
— not equity drawdown. Risk-adjusted (Sortino, downside-only), with confidence intervals
because the sample is small (~49 monthly windows).

## What

Produce an OOS profitability characterization of the **exact deployed canary config**
(`configs/cells.canary.yaml`): two cells, MeanReversion × fUST × {a30, p2}, with their
production params, each benchmarked against AlwaysFRR. Output a research doc with the
yield-native metric set, bootstrap CIs, a selection-bias deflation check, and a
non-backtestable-risk register.

**This pass sets no hard pass/fail gate.** It surfaces the real numbers. If a hard gate
is wanted afterward, thresholds are anchored to the observed baseline + worst-month
distribution — not to an invented `1.5%`.

Target cells (from `cells.canary.yaml`):

| Strategy | Symbol | period_agg | timeframe | params |
|---|---|---|---|---|
| mean_reversion | fUST | a30 | 1h | threshold_sigma=1.0, ratio_sigma=0.9915, ema_alpha=0.01183 |
| mean_reversion | fUST | p2  | 1h | threshold_sigma=1.0, ratio_sigma=0.9543, ema_alpha=0.01183 |

## Out of Scope

- **fUSD cells** — canary lends fUST-only (USDT funded); global allocation pool not yet
  per-currency (`per-currency-allocation-design.md`). Validate only what ships.
- **RatePercentile** — LOCF-disqualified (Phase 4.3), excluded from canary.
- **p30 cells** — excluded from canary config.
- **Hard ship gate / threshold** — deferred to a follow-up once numbers are in hand.
- **Tier-3 institutional machinery** — full Combinatorial Purged CV (CPCV) → PBO,
  formal Deflated Sharpe Ratio with full trial accounting, Monte Carlo path resampling,
  capacity analysis. Deferred: marginal rigor does not change a $450 sizing decision, and
  the binding catastrophic risk (platform/credit tail) is uncapturable by backtest. See
  Risks. Documented as the upgrade path when AUM becomes material.
- **Engine downside modeling** — no new opportunity-cost/default-haircut model in the
  engine this pass; idle is measured from trade/utilization counts, not simulated P&L.

## Methodology

### Rolling OOS evaluation (not re-optimization)

Reuse `compute_wfo_windows` (3-month train / 1-month test / 1-month step, post-2022) to
produce a sequence of calendar months. Params are **fixed at the deployed values** — we
do not re-fit per window. The train portion serves only as EMA **warmup**; each 1-month
test window yields one realized `net_monthly_return_pct`. The collection across ~49
windows is the empirical monthly-return distribution.

Per window, per cell:

```
slice = candles in [window.train_start_mts, window.test_end_mts]
result_strat = run_backtest(slice, MeanReversion(prod params),
                            record_start_mts=window.test_start_mts,
                            record_end_mts=window.test_end_mts)
result_base  = run_backtest(slice, AlwaysFRRStrategy(period_days=2),
                            record_start_mts=window.test_start_mts,
                            record_end_mts=window.test_end_mts)
```

A fresh strategy instance per window (stateful EMA). `observe()` runs over the warmup +
test slice; `decide()` fires only inside the test month — so each test candle's EMA
reflects only prior candles (no look-ahead within a window).

### OOS honesty caveat (must be stated in the output)

The deployed params were chosen by a sweep over **this same 2022–2026 history**. So these
per-month returns are **in-sample to the parameter-selection process** → an **optimistic**
estimate, not pristine OOS. This is exactly the selection bias the deflation check
quantifies. The only true out-of-sample test is the **live canary itself** — which is why
we validate before scaling. The doc states this prominently; the realized distribution is
labeled "realized historical performance of the deployed config (optimistic — in-sample
to selection)".

### Benchmark

AlwaysFRR (lend every candle at close rate, period_days=2) = the passive "always-on"
alternative, ≈ what a naïve Bitfinex auto-renew user gets. Net of the same 15% fee →
apples-to-apples. The active strategy must beat passive **net of the idle cost** its
selectivity incurs.

## Metrics (yield-native, replacing the dead gate)

Computed per cell, for both strategy and baseline, over the ~49 monthly windows:

| Category | Metric | Source |
|---|---|---|
| Absolute return | net monthly: median, p25, min (worst month), max, mean | per-window `net_monthly_return_pct` |
| Absolute return | compound annualized = ∏(1+mᵢ/100)^(12/N) − 1 | derived |
| vs passive | active monthly return = strat − baseline (median, distribution) | derived per window |
| vs passive | information ratio = mean(active) / std(active) | derived |
| Downside (reframed) | worst-month net (≥0 — "least-earning month", not a loss) | min |
| Downside (reframed) | idle rate = % windows with n_trades=0; mean fill_rate; trades(strat)/trades(base) deployment ratio | per-window `n_trades`, `fill_rate` |
| Risk-adjusted | Sortino (downside-deviation; reuse `sortino.py`); **Calmar omitted** (MDD≈0 → undefined) | per-window |
| Confidence | bootstrap 95% CI on: median monthly, mean active return, Sortino (resample windows with replacement, ~10k draws) | new pure fn |
| Selection bias | deflated-Sharpe sanity check given N_trials (configs tried in the sweep) | new pure fn |

`worst-month` (the min of a ≥0 distribution) replaces the dead drawdown floor as the
downside metric: it answers "in the leanest month over 4 years, what did we still earn?"

## Architecture

### Reused modules (no changes)

- `modules/backtest/wfo.py` — `compute_wfo_windows`
- `modules/backtest/engine.py` — `run_backtest` (record-window args already support OOS scoping)
- `modules/backtest/strategies/{mean_reversion,always_frr}.py`
- `modules/backtest/sortino.py` — Sortino primitive
- `modules/candles/repository.py` — `get_candles_in_range`

### New module: `modules/backtest/oos_profitability.py`

Pure, no I/O, fully unit-testable.

- `@dataclass(frozen=True) WindowOutcome` — `month_mts`, `net_monthly`, `n_trades`,
  `fill_rate`, `sortino` (one per window per arm).
- `@dataclass(frozen=True) OosSummary` — the metric set above (point estimates) for one arm.
- `summarize_oos(outcomes: list[WindowOutcome]) -> OosSummary` — distribution stats,
  compound annualized, idle rate, Sortino aggregation.
- `active_return_summary(strat: list[WindowOutcome], base: list[WindowOutcome]) -> ...`
  — paired active-return stats + information ratio (windows aligned by `month_mts`).
- `bootstrap_ci(values, stat_fn, n=10_000, alpha=0.05, seed=...) -> (lo, hi)` —
  percentile bootstrap; deterministic via seed.
- `deflated_sharpe(observed_sharpe, n_trials, n_obs, skew, kurtosis) -> Decimal` —
  Bailey & López de Prado deflated Sharpe; lightweight (closed-form), not full CPCV.

### New script: `scripts/run_oos_profitability.py`

CLI orchestration only (thin):

- Loads candles per cell from Neon (`get_candles_in_range`), post-2022.
- `compute_wfo_windows` → per-window `run_backtest` for strat + baseline → `WindowOutcome`s.
- Calls `summarize_oos` / `active_return_summary` / `bootstrap_ci` / `deflated_sharpe`.
- Writes `docs/research/2026-05-28-canary-oos-profitability.md` + `.json`.
- Params read from `cells.canary.yaml` (single source of truth); `ema_alpha → ema_span`
  mapped as `span = round(2/alpha − 1)` (0.01183 → 168).

## Output Format

`docs/research/2026-05-28-canary-oos-profitability.md`:

```
# Canary OOS Profitability — fUST MeanReversion (a30, p2)
Run date / data window / N windows per cell
## TL;DR — realized monthly median, annualized, active vs passive, worst-month, idle rate
## OOS honesty caveat  (in-sample-to-selection → optimistic; live canary = true OOS)
## Per-cell tables  (strat vs baseline: median/p25/worst/annualized, all with 95% CI)
## Active return vs passive  (information ratio, % months strat>base)
## Downside  (worst-month, idle rate, fill_rate, deployment ratio)
## Selection bias  (N configs tried; deflated Sharpe)
## Non-backtestable risk register  (platform/credit/liquidity → handled by cap, not here)
## Recommendation  (numbers-driven; whether/how to set a hard gate; scale-up readiness)
```

Plus `.json` sibling for machine consumption.

## Testing

TDD on the pure module (no DB):

- `summarize_oos`: synthetic windows with known values → exact median/p25/min/max,
  compound annualized, idle rate. Edge cases: all-zero windows (full idle), single window,
  empty list.
- `active_return_summary`: aligned paired windows → known active median + IR; misaligned
  `month_mts` → raises/skips per defined contract.
- `bootstrap_ci`: fixed seed → deterministic CI; degenerate (all equal) → lo==hi.
- `deflated_sharpe`: known textbook inputs → expected deflation; `n_trials=1` → ≈ raw
  probabilistic Sharpe; monotonic decrease as `n_trials` ↑.

Script-level: smoke that it runs end-to-end against Neon and emits both files (integration,
not in the `not integration` gate). Commit gate stays `pytest -m "not integration"` + mypy + ruff.

## Implementation Order

1. `oos_profitability.py` pure functions + unit tests (TDD).
2. `run_oos_profitability.py` script wiring.
3. **Strategy-parity check** (Risk #1): confirm backtest `MeanReversionStrategy` is
   semantically equivalent to the live marketfeed MR (ema_alpha↔ema_span, lower-band
   logic). If not equivalent, reconcile before trusting numbers.
4. Run against Neon → write research doc.
5. Present numbers to Will → decide on hard gate / scale-up.

## Risks & Open Questions

1. **Backtest-vs-live strategy parity.** `cells.canary.yaml` uses `ema_alpha`; the
   backtest `MeanReversionStrategy` takes `ema_span` and the live marketfeed strategy is a
   separate implementation. We map `ema_alpha→ema_span`, but must confirm both compute the
   same lower-band decision, or the validation doesn't reflect what's deployed. **Verify in
   step 3.**
2. **`ratio_sigma ≈ 0.99` may make MR ≈ AlwaysFRR.** lower_band = −1.0×0.9915 ≈ −0.99 means
   the strategy only pauses if close is ~99% below EMA — i.e. almost never. If so, the
   "active" return ≈ baseline and selectivity adds little. This is a finding the report will
   surface, not a blocker — but worth flagging that the deployed config may be near-passive.
3. **Selection bias is real and only partially quantified.** Deflated Sharpe is a sanity
   check, not a full PBO. True OOS = the live canary. Stated explicitly in the doc.
4. **Platform/credit tail is uncapturable.** Bitfinex insolvency / socialized loss /
   Tether risk is the actual catastrophic risk for a lending bot and **no backtest rigor
   addresses it** — it is mitigated by the position cap ($450) and not lending the full
   balance. The doc's risk register states this so the validation is not mistaken for
   safety it cannot provide.
5. **~49 monthly windows is a small sample.** Hence bootstrap CIs over point estimates;
   the doc must not over-claim precision.
