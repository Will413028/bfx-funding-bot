# Phase 3b EDA — Regime Drift Gate Failure

**Date**: 2026-05-18
**Status**: HALT — Phase 3b (single-split) invalidated
**Spec**: `docs/superpowers/specs/2026-05-17-phase3b-strategy-matrix-design.md`
**Window analyzed**: 2022-01-01 → 2026-05-18 UTC

## TL;DR

**All 6 cells failed the per-quarter regime drift gate** (threshold = 0.30, results 1.33 – 2.66). Per spec, this invalidates the single-split Phase 3b conclusions and triggers mandatory walk-forward upgrade. **Bonus finding**: WeekendPremium drop rule also fired on all 6 cells (effect sizes -0.47% to -10.96% — weekend rates are actually *lower* than weekday rates, refuting the strategy hypothesis entirely).

## Drift gate verdict (per-cell)

| Cell | Drift | Status |
|---|---|---|
| fUSD × p2 | 1.328 (133%) | FAIL |
| fUSD × p30 | 2.192 (219%) | FAIL |
| fUSD × a30 | 1.547 (155%) | FAIL |
| fUST × p2 | 2.621 (262%) | FAIL |
| fUST × p30 | 1.989 (199%) | FAIL |
| fUST × a30 | 2.662 (266%) | FAIL |

`Drift = (max_quarter_mean - min_quarter_mean) / min_quarter_mean` computed on per-cell train portion (first 70% of post-2022 candles).

## Weekend effect (refutes WeekendPremium hypothesis)

| Cell | (Fri+Sat+Sun avg − Mon-Thu avg) / Mon-Thu avg |
|---|---|
| fUSD × p2 | −0.47% |
| fUSD × p30 | +0.30% |
| fUSD × a30 | −0.60% |
| fUST × p2 | −6.82% |
| fUST × p30 | **−10.96%** |
| fUST × a30 | −5.81% |

Weekends have **lower** funding rates than weekdays in 4 of 6 cells. The remaining 2 are sub-1% effects (noise). Folklore that "crypto weekends spike funding demand" is not supported by post-2022 data — WeekendPremium can be dropped from any future strategy set unless we adopt a regime-specific framing.

## Why the gate failed

Phase 3a found per-year FRR slope swing 9× across 2016-2026. We chose post-2022 hoping it was a homogeneous regime. **The data refutes this**: within post-2022, per-quarter drift remains 1.3–2.7×. Likely driver: **Fed 2022-2024 rate hike cycle** restructured the relationship between traditional yields and crypto borrowing demand:

- 2022 H1: low Fed rates → crypto borrowing demand high → funding rates elevated
- 2023+: Fed terminal rates ~5% → traditional yields competitive → crypto funding demand structurally shifted

This is **real structural non-stationarity**, not measurement noise.

## Full per-cell statistics (informational)

### fUSD × p2

- n_candles (post-2022): 38,153; train: 26,707; train_end_mts: 1,737,165,600,000 (2025-01-17 22:00 UTC)
- Percentiles: P25=0.00011, P50=0.00015, P75=0.00021, P90=0.00029
- close/EMA(24h) σ: 0.346; close/EMA(168h) σ: 0.455
- ACF: {1h: 0.49, 24h: 0.33, 168h: 0.23, 720h: 0.13}

### fUSD × p30

- n_candles: 25,598; train: 17,918
- Percentiles: P25=0.00021, P50=0.00031, P75=0.00050, P90=0.00069
- close/EMA(24h) σ: 0.308; close/EMA(168h) σ: 0.602
- ACF: {1h: 0.13, 24h: 0.08, 168h: 0.05, 720h: 0.03}

### fUSD × a30

- n_candles: 38,157; train: 26,709
- Percentiles: P25=0.00011, P50=0.00015, P75=0.00022, P90=0.00030
- close/EMA(24h) σ: 0.377; close/EMA(168h) σ: 0.480
- ACF: {1h: 0.46, 24h: 0.32, 168h: 0.24, 720h: 0.14}

### fUST × p2

- n_candles: 38,158; train: 26,710
- Percentiles: P25=0.00012, P50=0.00018, P75=0.00025, P90=0.00036
- close/EMA(24h) σ: 0.408; close/EMA(168h) σ: 0.954
- ACF: {1h: 0.10, 24h: 0.03, 168h: 0.008, 720h: 0.007}

### fUST × p30

- n_candles: 17,732; train: 12,412 (sample smaller than other cells)
- Percentiles: P25=0.00023, P50=0.00030, P75=0.00040, P90=0.00050
- close/EMA(24h) σ: 0.451; close/EMA(168h) σ: **1.406** (highest)
- ACF: {1h: 0.10, 24h: 0.009, 168h: −0.002, 720h: −0.001}

### fUST × a30

- n_candles: 38,164; train: 26,714
- Percentiles: P25=0.00012, P50=0.00018, P75=0.00026, P90=0.00037
- close/EMA(24h) σ: 0.422; close/EMA(168h) σ: 0.992
- ACF: {1h: 0.08, 24h: 0.04, 168h: 0.01, 720h: 0.01}

## Cross-cell observations

- **fUSD vs fUST persistence asymmetry**: fUSD ACF(1h) ≈ 0.46-0.49 (moderate persistence); fUST ACF(1h) ≈ 0.08-0.13 (near-noise). fUST market is much "thinner" / more random walk-like at hourly resolution.
- **EMA volatility ranges widely**: σ from 0.31 (fUSD p30 EMA24) to 1.41 (fUST p30 EMA168). Wider σ in fUST suggests larger short-term swings around the trend.
- **Sample size disparity**: fUST p30 only 17.7k post-2022 candles vs 38k for others. Possibly later p30 candle availability; worth checking before WFO assumes uniform coverage.

## Decision

Per Phase 3b spec section "Risks & Open Questions":
> Within-post-2022 仍有 regime drift（per-quarter mean rate 變動 ≥ 30%） → EDA Step 5 偵測 → 觸發即升級 mandatory WFO，Phase 3b 結論作廢

**Phase 3b (single 70/30 split) HALTED.** Proceeding to redesign Phase 3b as Phase 3b-WFO with walk-forward CV. Next steps:

1. New spec: `docs/superpowers/specs/2026-05-18-phase3b-wfo-strategy-matrix-design.md`
2. Reuse Phase A foundations (split helper, sortino, engine record window, observe routing, eda module) — these survive the methodology change unchanged.
3. Reuse strategy implementations (RatePercentile / MeanReversion) — survive unchanged. **WeekendPremium dropped entirely** per evidence above.
4. New WFO matrix runner replaces single-split matrix runner.
5. New WFO results format aggregates per-window OOS metrics + checks stability across windows.

## Reusable artifacts (Phase A + B + C scope-survivors)

| Commit | Artifact | WFO reuse? |
|---|---|---|
| `a6ed207` + `c67c40d` | `compute_train_end_mts` | Adapted: WFO uses many train/test windows, not a single split. Will likely add `compute_wfo_windows(candles, train_months, test_months, step_months)` helper. |
| `51b24f5` + `bc8597b` | Sortino module | Reused as-is. |
| `7c2c673` | Strategy.observe + param_grid_for_cell | Reused as-is. |
| `af5f082` + `4ab9282` | Engine record window + Sortino integration | Reused as-is — record_start_mts / record_end_mts is exactly what WFO needs to scope each window. |
| `0bf9ae4` + `7c02778` | EDA module + script | Reused as-is. EDA per-cell stats inform per-window WFO param grids. |
