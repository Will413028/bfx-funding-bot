# Signal EDA Funnel — Design Spec

**Date**: 2026-06-05
**Status**: Design approved, pending plan
**Type**: Offline strategy research (no live/canary/engine impact)

## Context

The strategy library has 4 strategies (MeanReversion deployed, RatePercentile,
AlwaysMarketRate baseline, AdaptivePeriod new). All of them time their decisions
off a single signal: rate **deviation-from-EMA** (`(close−EMA)/EMA`). ROADMAP
Phase 3c lists several untested signal hypotheses (FRR-trend, SpikeDetect,
funding_amount covariate) that have never been validated.

The recurring failure mode in this project is **backtest edge that evaporates
live** (band-sweep load-bearing lesson; live G3 MR-alpha ≈ 0). The matching
discipline is: a new signal must clear a cheap **effect-size + stability** bar
*before* it earns a full strategy implementation — exactly how WeekendPremium
was killed by EDA (weekend effect 4/6 cells negative) before any sweep ran.

This spec defines an **EDA-first funnel**: score candidate signals for
predictive power against forward market-rate movement, emit a GO/KILL list, and
only promote survivors to full strategies in a later sub-project.

### Premise check (verified)

- `funding_stats` table has `frr`, `avg_period`, `funding_amount`,
  `funding_amount_used`, `funding_below_threshold` — all backfilled
  (~547k rows across 8 series, 2016–2026).
- The backtest `engine` currently consumes only `funding_candles` (close =
  market rate; see `engine.py` comment referencing the FRR-decoupling spec).
  FRR / funding_amount series are **not** wired into the engine.
- **Consequence**: EDA runs as lightweight DB-query + pandas analysis, NOT
  through the engine. Engine plumbing is deferred to the promotion step (only
  for signals that pass). This is the core benefit of EDA-first: heavy work
  comes after validation, not before.

## Goal

Produce a research report that, for each of 4 candidate signals, answers:
**does it predict forward market-rate movement with a stable, regime-robust
effect, after multiple-testing correction?** Output is a GO/KILL verdict list.

Non-goal: building strategies, engine plumbing, WFO, or any deploy step.

## ① Forward-return target

The offer rate is pinned to `close_t` (the fill model makes any other rate
net-negative), so a signal's value is not "how much interest" (already locked)
but **guiding gating (lend or not) and period (how long to lock)** — both of
which hinge on where the market rate goes next.

- **target** = forward rate change
  `Δ_H = mean(close[t+1 : t+H]) − close_t`,
  for horizons `H ∈ {2, 7, 14, 30}` days (spanning the p2 short-lock to a30/p30
  long-lock decision scales).
- `Δ_H > 0` → rate will rise (short-lock + re-price often, or gate-and-wait
  pays); `Δ_H < 0` → rate will fall (long-lock to capture the current high —
  the AdaptivePeriod thesis).
- Source: daily `funding_candles.close` (canonical `market_rate_source`),
  computed per cell (fUST/fUSD × a30/p2).

The EDA effectively asks: **do these 4 signals predict `Δ_H` better than the
deviation-from-EMA signal AdaptivePeriod already uses?**

## ② Signal forms + go/kill thresholds

All signals are **rolling/relative — absolute values are forbidden**, because
FRR carries per-year slope drift up to 9× (Bitfinex changed FRR methodology
over time; see time-series-regression-cv-pitfall). Absolute forms would be
contaminated by regime drift rather than measuring genuine predictive structure.

| Signal | Form |
|---|---|
| FRR-trend | `ΔFRR_k = (FRR_t − FRR_{t−k}) / rolling_std`, k ∈ {1, 3, 7} |
| SpikeDetect | `z = (FRR_t − rollmean_w) / rollstd_w`, w ∈ {14, 30} |
| funding_amount | rolling percentile rank + Δ trend |
| utilization | `funding_amount_used / funding_amount` rolling percentile + Δ |

### Metrics

- **Primary** = Spearman IC = rank-corr(`signal_t`, `Δ_H`), computed across
  `H × cell × 2 disjoint regimes` (2016–21 vs 2022+, **not** nested — nested
  windows are auto-correlated and blind to post-2022 regime overfit, per the
  band-sweep disjoint-windows lesson).
- **Secondary** = quintile-spread: top-20% vs bottom-20% signal buckets, their
  mean `Δ_H` difference + monotonicity across the 5 buckets (the WeekendPremium
  method — direct, catches non-linearity).
- **Significance** = block-bootstrap CI on IC (block resampling preserves
  autocorrelation; plain bootstrap would overstate significance on serially
  correlated series).
- **Multiple-testing** = Benjamini-Hochberg FDR across ALL
  (signal × H × cell × regime) tests (dozens of tests → some will pass by luck
  without correction; this is the heart of the WeekendPremium lesson).

### GO / KILL

- **GO** = IC significant (FDR-adjusted) AND same sign across both regimes AND
  same sign across ≥3/4 cells (majority, band-sweep discipline) AND
  `|median IC| ≥ 0.03` (a defensible floor for low-SNR funding data).
- **KILL** = IC near 0 / sign flips across regimes (regime-fragile) /
  significant in only a single cell.
- **Null result is first-class** — all 4 signals killed is a valid, publishable
  conclusion. Do NOT manufacture a champion (the fill-sweep and WeekendPremium
  precedents).

## ③ Output + implementation

- **Output**: `docs/research/YYYY-MM-DD-signal-eda-funnel.md` — per signal: IC
  table (H × cell × regime), quintile numbers, bootstrap CI, GO/KILL verdict,
  and (for survivors) a promotion recommendation. Plus a `.json` sidecar
  mirroring existing research reports.
- **Implementation**:
  - new module `modules/backtest/signal_eda.py` — signal transforms, IC,
    quintile-spread, block-bootstrap, BH-FDR, GO/KILL decision.
  - driver `scripts/run_signal_eda.py` — queries `funding_stats` +
    `funding_candles` directly, **read-only SELECT**, live Neon.
- **TDD**: each transform / IC / quintile / block-bootstrap / FDR tested against
  synthetic data with a known IC (inject a series with planted correlation,
  assert it is recovered). Guard tests: regime split is genuinely disjoint
  (no shared rows); FDR actually adjusts p-values (a marginal raw-p that passes
  un-adjusted gets rejected adjusted); absolute-value forms are not used
  (rolling stats only).
- **Does NOT touch** engine / live path / canary. Pure offline research,
  consistent with existing `oos_profitability` / `band_sweep`.

## Scope boundaries (YAGNI)

- Signal screening ONLY — no strategy class, no engine plumbing, no WFO. Those
  belong to a follow-up sub-project gated on a GO verdict.
- No new data sources — only the already-backfilled `funding_stats` +
  `funding_candles`.

## Downstream (out of scope here)

For each GO signal, a separate spec → plan → impl cycle: wire the funding_stats
series into the engine, build a strategy class expressing the signal, and run it
through WFO + OOS (the same gauntlet AdaptivePeriod cleared). KILL signals are
recorded as falsified and removed from the candidate pool permanently.
