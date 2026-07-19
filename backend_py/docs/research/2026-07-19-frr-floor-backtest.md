# MR-with-FRR-floor backtest — SKIP FRR parking probe

**Run date**: 2026-07-19T10:50:52.470772+00:00
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
| annualized net % | 7.70 | 5.85 | 6.78 | 1.83 |
| median monthly % | 0.5701 | 0.4492 | 0.5112 | 0.1123 |
| mean monthly % | 0.6205 | 0.4750 | 0.5487 | 0.1512 |
| p25 monthly % | 0.4566 | 0.3136 | 0.3930 | 0.0376 |
| worst month % | 0.0614 | 0.0404 | 0.0545 | 0.0000 |
| best month % | 1.9717 | 1.8368 | 1.9073 | 0.7282 |
| idle rate | 0.00% | 0.00% | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 0.7641 | 1.0000 | 0.1908 |
| sortino | Infinity | Infinity | Infinity | Infinity |

### Paired monthly differences (a - b, % per month)

| pair | median | mean [95% CI] | IR | months a>b (win rate) |
|---|---|---|---|---|
| mr_frr_floor_vs_mr | -0.1165 | -0.1454 [-0.1896, -0.1032] | -0.7136141219463130898044842789 | 3.45% |
| mr_frr_floor_vs_always_frr | 0.2944 | 0.3238 [0.2858, 0.3651] | 1.710653668470357389648600648 | 100.00% |
| mr_frr_floor_vs_always_market_rate | -0.0584 | -0.0736 [-0.0871, -0.0608] | -1.184087710987872751271251737 | 0.00% |
| mr_vs_always_market_rate | 0.0347 | 0.0718 [0.0350, 0.1108] | 0.3996274117804254123485897420 | 74.71% |
| always_frr_vs_always_market_rate | -0.3834 | -0.3974 [-0.4404, -0.3581] | -1.999741918876790614775228294 | 0.00% |

### Per-year mean net monthly % (regime drift check)

| year | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| 2019 | 0.9034 | 0.7488 | 0.7937 | 0.3012 |
| 2020 | 0.6473 | 0.6589 | 0.7100 | 0.2939 |
| 2021 | 0.6472 | 0.4497 | 0.5538 | 0.1651 |
| 2022 | 0.4973 | 0.2710 | 0.3414 | 0.0724 |
| 2023 | 0.7301 | 0.4720 | 0.6016 | 0.1300 |
| 2024 | 0.5883 | 0.4778 | 0.5393 | 0.1401 |
| 2025 | 0.4979 | 0.4278 | 0.4673 | 0.0560 |
| 2026 | 0.4253 | 0.2504 | 0.3381 | 0.0257 |

## fUSD — four arms over 111 monthly OOS windows

Data window: 2016-12-05 .. 2026-07-19

| metric | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| annualized net % | 9.80 | 7.89 | 8.99 | 3.24 |
| median monthly % | 0.5947 | 0.4352 | 0.5339 | 0.0829 |
| mean monthly % | 0.7838 | 0.6369 | 0.7215 | 0.2668 |
| p25 monthly % | 0.4093 | 0.2923 | 0.3930 | 0.0112 |
| worst month % | 0.1101 | 0.0687 | 0.0983 | 0.0000 |
| best month % | 3.8233 | 3.8233 | 3.8233 | 2.5780 |
| idle rate | 0.00% | 0.00% | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 0.7865 | 1.0000 | 0.1965 |
| sortino | Infinity | Infinity | Infinity | Infinity |

### Paired monthly differences (a - b, % per month)

| pair | median | mean [95% CI] | IR | months a>b (win rate) |
|---|---|---|---|---|
| mr_frr_floor_vs_mr | -0.0930 | -0.1469 [-0.1775, -0.1180] | -0.9139389531043496298202439137 | 4.50% |
| mr_frr_floor_vs_always_frr | 0.3404 | 0.3701 [0.3325, 0.4103] | 1.760713845183723935784630649 | 100.00% |
| mr_frr_floor_vs_always_market_rate | -0.0666 | -0.0846 [-0.0987, -0.0712] | -1.149021666771748181611847233 | 0.00% |
| mr_vs_always_market_rate | 0.0317 | 0.0623 [0.0424, 0.0838] | 0.5640863821269267947729964326 | 78.38% |
| always_frr_vs_always_market_rate | -0.4003 | -0.4547 [-0.4997, -0.4122] | -1.952663495143054675418457942 | 0.00% |

### Per-year mean net monthly % (regime drift check)

| year | mr | mr_frr_floor | always_market_rate | always_frr |
|---|---|---|---|---|
| 2017 | 2.3498 | 2.0972 | 2.2377 | 1.3857 |
| 2018 | 0.6816 | 0.6044 | 0.6512 | 0.2486 |
| 2019 | 0.8956 | 0.8538 | 0.9124 | 0.4869 |
| 2020 | 0.9777 | 0.8352 | 0.9335 | 0.3911 |
| 2021 | 0.5785 | 0.4439 | 0.5133 | 0.1135 |
| 2022 | 0.6113 | 0.3230 | 0.4263 | 0.0506 |
| 2023 | 0.6005 | 0.4098 | 0.5195 | 0.0241 |
| 2024 | 0.4258 | 0.3272 | 0.4002 | 0.0413 |
| 2025 | 0.5129 | 0.3726 | 0.4503 | 0.0557 |
| 2026 | 0.4082 | 0.2967 | 0.3781 | 0.0341 |

## Honesty caveats

- **Fill model is the linear proxy** (alpha=5): offers priced above the candle close get linearly discounted fill, hitting 0 fill at +20% above close. FRR frequently sits far above the p2 close (median close/(frr*365) ~0.33-0.6 since 2022), so the FRR arms' offers are often modeled as 0-fill months — the real FRR auto-renew queue would eventually fill at FRR. This systematically *understates* always_frr and mr_frr_floor. There is no validated fill model for FRR-pegged offers; treat the FRR arms under alpha=5 as a lower bound, and the --fill-alpha 1e-9 sensitivity run ('FRR offers always fill at FRR') as an upper bound.
- **Immediate-execution assumption**: a decision fills (probabilistically) at the decision candle, then capital locks for period 2d + 30min gap. No queue latency, no partial-period returns.
- **15% Bitfinex fee applied uniformly** to all arms (net figures); real FRR auto-renew pays the same fee, so pairwise comparisons are fee-neutral.
- **Deployed MR params were selected on this same history** (Phase 3b sweep) — MR/floor arms are in-sample to parameter selection; the passive arms are not. Optimistic for MR-family arms.
- **funding_stats.frr is hourly, LOCF as-of lookup** (staleness cap 24h; max observed gap 9.2h). FRR before the funding_stats series start is unavailable -> those candles are idle for FRR arms (start is clamped to joint coverage, so this only affects edges).
- **Full-equity compounding**: each trade deploys the whole budget; no per-cell allocation, no order-book depth, no chunking. Same simplification for all arms.
- **p2 cells only** (deployed canary uses a30+p2; a30's FRR-anchored period semantics are not modeled here).
## Conclusion (synthesis, added post-run — reads this file + the `-frrfill1` sensitivity run)

**FRR-floor does NOT robustly dominate. The answer is entirely fill-assumption-bound, and the two bounds
bracket zero — this is precisely the question only E3 live measurement can close.**

- **Lower bound (this file, linear fill alpha=5 — FRR offers above close get decayed fill):**
  mr_frr_floor **loses** to plain MR on both symbols (mean −0.145 %/mo fUST, CI [−0.190, −0.103];
  −0.147 %/mo fUSD, CI [−0.178, −0.118]; floor wins only 3-5% of months). Mechanism: parking locks
  capital 2d at a mean fill of ~0.19-0.20 during skips (FRR sits 1.7-3x above p2 close since 2022 →
  mostly 0-fill), while plain MR stays liquid and re-enters at recovered rates next candle.
  AlwaysFRR is crushed (1.8% / 3.2% ann. net).
- **Upper bound (`2026-07-19-frr-floor-backtest-frrfill1.md`, --fill-alpha 1e-9 — FRR offers always
  fill at FRR):** mr_frr_floor **beats** MR (+0.076 %/mo fUST, CI [0.032, 0.119]; +0.104 %/mo fUSD,
  CI [0.080, 0.131]; wins 77-78% of months) — but then **AlwaysFRR beats everything** (11.1% / 14.4%
  ann. net vs MR's 7.7% / 9.8%; beats AMR in 94-98% of months), i.e. under that assumption the whole
  MR timing layer is negative-value vs plain FRR auto-renew and the product thesis, not just the skip
  branch, is what's under test.
- **The 2026-07-06 contradiction is reproduced and explained**: MR's skip edge vs AlwaysMarketRate is
  real in-model on both symbols (mean +0.072 / +0.062 %/mo, 95% CI fully positive, ~8-13% relative —
  the WFO "+15-19%" claim's shape), but it is exactly the months where FRR parking would out-earn
  idle IF FRR fills. Live G3 "alpha ≈ 0" is consistent with reality sitting between the two bounds.
- **Decision**: do not promote MR-with-FRR-floor on this backtest. The discriminating datum is the
  realized fill behavior of FRR-pegged offers — E3's continuous attribution (bot / always-close /
  AlwaysFRR three-line dashboard) plus, if desired, a small real-money FRR-parked probe, will pick
  the branch: if live FRR fills are good, the right move per this model is *more* FRR exposure (and a
  re-examination of MR's value vs auto-renew), not just a floor on the skip branch.
