# MR-with-FRR-floor backtest — SKIP FRR parking probe

**Run date**: 2026-07-19T10:51:41.213895+00:00
**Data window**: fUST: 2018-12-21 .. 2026-07-19 / fUSD: 2016-12-05 .. 2026-07-19
**Methodology**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), linear fill model, deployed canary MR params (fixed, not re-swept). Same window methodology as run_oos_profitability.py.
**Question** (profit review 2026-07-06 §3): does replacing MR's SKIP-idle with FRR parking dominate — i.e. reconcile 'WFO says skip is +15-19% edge' with 'live G3 says MR alpha ~= 0'?

## FRR unit audit (gate: passed for every symbol below)

- `funding_stats.frr` is stored as (per-day FRR)/365 (~1e-6 scale; the 2026-05-10 sample 1.12e-06 is this normal scale, not a corrupt value). Per-day rate = `frr x 365` (FRR_ANNUALIZATION SSOT, live-verified 2026-07-06 vs ticker FRR, <0.5% error).
- Gate: per-year median close/(frr*365) must lie in [0.1, 10] — a unit error would be ~365x off. Observed drift 0.93 -> 0.33 across 2016-2026 is market structure (FRR premium vs p2 close grew), not units.

### fUST

| year | n | median frr (stored) | median close | median close/(frr*365) |
|---|---|---|---|---|
| 2018 | 187 | 2.090E-6 | 4.334E-4 | 0.7633 |
| 2019 | 7935 | 7.400E-7 | 1.949E-4 | 0.8219 |
| 2020 | 8780 | 8.600E-7 | 2.251E-4 | 0.8830 |
| 2021 | 8756 | 9.200E-7 | 1.989E-4 | 0.5695 |
| 2022 | 8756 | 8.200E-7 | 1.000E-4 | 0.4723 |
| 2023 | 8759 | 1.230E-6 | 2.190E-4 | 0.5708 |
| 2024 | 8782 | 9.100E-7 | 1.864E-4 | 0.5449 |
| 2025 | 8760 | 8.400E-7 | 1.752E-4 | 0.5909 |
| 2026 | 4784 | 8.700E-7 | 1.369E-4 | 0.4646 |

### fUSD

| year | n | median frr (stored) | median close | median close/(frr*365) |
|---|---|---|---|---|
| 2016 | 604 | 2.070E-6 | 7.370E-4 | 0.9686 |
| 2017 | 8705 | 2.150E-6 | 6.886E-4 | 0.9255 |
| 2018 | 8757 | 5.700E-7 | 1.616E-4 | 0.8191 |
| 2019 | 8731 | 1.020E-6 | 3.074E-4 | 0.9009 |
| 2020 | 8784 | 1.180E-6 | 3.148E-4 | 0.8450 |
| 2021 | 8758 | 6.300E-7 | 1.370E-4 | 0.6368 |
| 2022 | 8754 | 8.300E-7 | 1.302E-4 | 0.4059 |
| 2023 | 8760 | 1.630E-6 | 1.915E-4 | 0.3303 |
| 2024 | 8782 | 8.800E-7 | 1.300E-4 | 0.4193 |
| 2025 | 8760 | 1.020E-6 | 1.588E-4 | 0.4337 |
| 2026 | 4775 | 1.190E-6 | 1.400E-4 | 0.3341 |

## fUST — four arms over 87 monthly OOS windows

Data window: 2018-12-21 .. 2026-07-19

| metric | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| annualized net % | 7.70 | 8.68 | 6.78 | 11.12 |
| median monthly % | 0.5701 | 0.6449 | 0.5112 | 0.8216 |
| mean monthly % | 0.6205 | 0.6963 | 0.5487 | 0.8835 |
| p25 monthly % | 0.4566 | 0.4996 | 0.3930 | 0.6566 |
| worst month % | 0.0614 | 0.1630 | 0.0545 | 0.2748 |
| best month % | 1.9717 | 2.0536 | 1.9073 | 2.2902 |
| idle rate | 0.00% | 0.00% | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| sortino | Infinity | Infinity | Infinity | Infinity |

### Paired monthly differences (a - b, % per month)

| pair | median | mean [95% CI] | IR | months a>b (win rate) |
|---|---|---|---|---|
| mr_frr_floor_vs_mr | 0.0464 | 0.0758 [0.0317, 0.1190] | 0.3669126178991324430752786635 | 77.01% |
| mr_frr_floor_vs_always_frr | -0.1512 | -0.1872 [-0.2373, -0.1345] | -0.7698626706554118951655578528 | 6.90% |
| mr_frr_floor_vs_always_market_rate | 0.1173 | 0.1476 [0.1185, 0.1789] | 1.033242517794699640416422143 | 89.66% |
| mr_vs_always_market_rate | 0.0347 | 0.0718 [0.0350, 0.1108] | 0.3996274117804254123485897420 | 74.71% |
| always_frr_vs_always_market_rate | 0.2706 | 0.3348 [0.2673, 0.4048] | 1.034688824850606028899520829 | 97.70% |

### Per-year mean net monthly % (regime drift check)

| year | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| 2019 | 0.9034 | 0.9127 | 0.7937 | 1.0959 |
| 2020 | 0.6473 | 0.7529 | 0.7100 | 0.7700 |
| 2021 | 0.6472 | 0.7393 | 0.5538 | 0.8958 |
| 2022 | 0.4973 | 0.5777 | 0.3414 | 0.7597 |
| 2023 | 0.7301 | 0.8518 | 0.6016 | 1.0890 |
| 2024 | 0.5883 | 0.6311 | 0.5393 | 0.8579 |
| 2025 | 0.4979 | 0.5345 | 0.4673 | 0.8157 |
| 2026 | 0.4253 | 0.5522 | 0.3381 | 0.7903 |

## fUSD — four arms over 111 monthly OOS windows

Data window: 2016-12-05 .. 2026-07-19

| metric | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| annualized net % | 9.80 | 11.16 | 8.99 | 14.44 |
| median monthly % | 0.5947 | 0.6935 | 0.5339 | 1.0137 |
| mean monthly % | 0.7838 | 0.8877 | 0.7215 | 1.1328 |
| p25 monthly % | 0.4093 | 0.5095 | 0.3930 | 0.7003 |
| worst month % | 0.1101 | 0.1220 | 0.0983 | 0.1436 |
| best month % | 3.8233 | 3.8363 | 3.8233 | 4.1097 |
| idle rate | 0.00% | 0.00% | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| sortino | Infinity | Infinity | Infinity | Infinity |

### Paired monthly differences (a - b, % per month)

| pair | median | mean [95% CI] | IR | months a>b (win rate) |
|---|---|---|---|---|
| mr_frr_floor_vs_mr | 0.0625 | 0.1039 [0.0802, 0.1305] | 0.7758058985940341364656660863 | 78.38% |
| mr_frr_floor_vs_always_frr | -0.1897 | -0.2451 [-0.2918, -0.1988] | -0.9710869885252692094331886425 | 7.21% |
| mr_frr_floor_vs_always_market_rate | 0.1089 | 0.1662 [0.1339, 0.2025] | 0.8995995048489437107048727083 | 90.99% |
| mr_vs_always_market_rate | 0.0317 | 0.0623 [0.0424, 0.0838] | 0.5640863821269267947729964326 | 78.38% |
| always_frr_vs_always_market_rate | 0.3272 | 0.4112 [0.3466, 0.4779] | 1.156935925058005024803140145 | 93.69% |

### Per-year mean net monthly % (regime drift check)

| year | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| 2017 | 2.3498 | 2.3735 | 2.2377 | 2.4974 |
| 2018 | 0.6816 | 0.6977 | 0.6512 | 0.8008 |
| 2019 | 0.8956 | 0.9667 | 0.9124 | 0.9994 |
| 2020 | 0.9777 | 1.0478 | 0.9335 | 1.1313 |
| 2021 | 0.5785 | 0.6315 | 0.5133 | 0.7563 |
| 2022 | 0.6113 | 0.7671 | 0.4263 | 1.1176 |
| 2023 | 0.6005 | 0.8739 | 0.5195 | 1.5325 |
| 2024 | 0.4258 | 0.5355 | 0.4002 | 0.8358 |
| 2025 | 0.5129 | 0.5817 | 0.4503 | 0.8870 |
| 2026 | 0.4082 | 0.6587 | 0.3781 | 1.0888 |

## Honesty caveats

- **Fill model is the linear proxy** (alpha=5): offers priced above the candle close get linearly discounted fill, hitting 0 fill at +20% above close. FRR frequently sits far above the p2 close (median close/(frr*365) ~0.33-0.6 since 2022), so the FRR arms' offers are often modeled as 0-fill months — the real FRR auto-renew queue would eventually fill at FRR. This systematically *understates* always_frr and mr_frr_floor. There is no validated fill model for FRR-pegged offers; treat the FRR arms under alpha=5 as a lower bound, and the --fill-alpha 1e-9 sensitivity run ('FRR offers always fill at FRR') as an upper bound.
- **Immediate-execution assumption**: a decision fills (probabilistically) at the decision candle, then capital locks for period 2d + 30min gap. No queue latency, no partial-period returns.
- **15% Bitfinex fee applied uniformly** to all arms (net figures); real FRR auto-renew pays the same fee, so pairwise comparisons are fee-neutral.
- **Deployed MR params were selected on this same history** (Phase 3b sweep) — MR/floor arms are in-sample to parameter selection; the passive arms are not. Optimistic for MR-family arms.
- **funding_stats.frr is hourly, LOCF as-of lookup** (staleness cap 24h; max observed gap 9.2h). FRR before the funding_stats series start is unavailable -> those candles are idle for FRR arms (start is clamped to joint coverage, so this only affects edges).
- **Full-equity compounding**: each trade deploys the whole budget; no per-cell allocation, no order-book depth, no chunking. Same simplification for all arms.
- **p2 cells only** (deployed canary uses a30+p2; a30's FRR-anchored period semantics are not modeled here).