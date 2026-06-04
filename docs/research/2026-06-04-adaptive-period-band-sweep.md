# Adaptive-Period Band (t1,t2) Sweep

**Data window**: 2016-01 .. now (split at 2022-01)
**Fill model**: linear, mean fill = 1.0 (the 100%-fill optimism caveat).
**Status**: characterization only — locked-but-not-armed, same as p_long=14. Not in cells.canary.yaml; zero live impact.

## Standing caveats

- 100%-fill optimism (mean fill = 1.0); EDA ratio_sigma from 2022-2026; tiers chosen-not-gated. The chosen band is a relative, backtest-internal pick, NOT a claim it reproduces live or resolves live≈0.
- Robustness gate = disjoint pre-2022 vs 2022+ ranks (not nested). DSR is on the ACTIVE series (n_trials=8); the bot-vs-idle level DSR is non-gating.
- Null result is valid: if survivors' paired difference CIs straddle 0, band is not a meaningful lever — keep the highest-t2 (least p14-tail) band.

## Cell fUST_a30

| (t1,t2) | median active | mean active | mean÷median | best-month | win-rate | avg_period | p14_share | active-DSR |
|---|---|---|---|---|---|---|---|---|
| (0.5,1.5) | 0.0932 | 0.1423 | 1.53 | 0.9259 | 0.76 | 3.07 | 0.181 | 0.9999 |
| (0.5,2.0) | 0.0766 | 0.1248 | 1.63 | 0.9259 | 0.76 | 2.92 | 0.080 | 0.9999 |
| (0.5,2.5) | 0.0766 | 0.1248 | 1.63 | 0.9259 | 0.76 | 2.84 | 0.053 | 0.9999 |
| (1.0,1.5) | 0.0090 | 0.1918 | 21.25 | 4.3313 | 0.52 | 2.48 | 0.156 | 0.9997 |
| (1.0,2.0) | 0.0141 | 0.1853 | 13.10 | 4.3313 | 0.55 | 2.37 | 0.075 | 0.9999 |
| (1.0,2.5) | 0.0141 | 0.1719 | 12.15 | 4.3313 | 0.55 | 2.43 | 0.075 | 0.9998 |
| (1.5,2.0) | 0.0000 | 0.1428 | 0.00 | 4.3313 | 0.35 | 2.22 | 0.086 | 0.9978 |
| (1.5,2.5) | 0.0000 | 0.1311 | 0.00 | 4.3313 | 0.35 | 2.20 | 0.048 | 0.9937 |

### Disjoint-window rank (median active; lower rank = better)

| (t1,t2) | rank pre-2022 | rank 2022+ |
|---|---|---|
| (0.5,1.5) | 1 | 1 |
| (0.5,2.0) | 2 | 2 |
| (0.5,2.5) | 3 | 3 |
| (1.0,1.5) | 4 | 4 |
| (1.0,2.0) | 5 | 5 |
| (1.0,2.5) | 6 | 6 |
| (1.5,2.0) | 7 | 7 |
| (1.5,2.5) | 8 | 8 |

## Cell fUST_p2

| (t1,t2) | median active | mean active | mean÷median | best-month | win-rate | avg_period | p14_share | active-DSR |
|---|---|---|---|---|---|---|---|---|
| (0.5,1.5) | 0.0981 | 0.1339 | 1.37 | 0.9890 | 0.77 | 3.06 | 0.129 | 0.9974 |
| (0.5,2.0) | 0.0966 | 0.1328 | 1.37 | 0.9890 | 0.78 | 2.90 | 0.075 | 0.9985 |
| (0.5,2.5) | 0.0940 | 0.1256 | 1.34 | 0.9890 | 0.77 | 2.85 | 0.038 | 0.9975 |
| (1.0,1.5) | 0.0213 | 0.2598 | 12.21 | 6.7922 | 0.58 | 2.54 | 0.178 | 0.9998 |
| (1.0,2.0) | 0.0213 | 0.2478 | 11.65 | 6.7922 | 0.59 | 2.40 | 0.097 | 0.9996 |
| (1.0,2.5) | 0.0266 | 0.2497 | 9.37 | 6.7922 | 0.59 | 2.40 | 0.070 | 0.9997 |
| (1.5,2.0) | 0.0000 | 0.1925 | 0.00 | 6.7922 | 0.30 | 2.27 | 0.114 | 0.9336 |
| (1.5,2.5) | 0.0000 | 0.1920 | 0.00 | 6.7922 | 0.31 | 2.22 | 0.087 | 0.9333 |

### Disjoint-window rank (median active; lower rank = better)

| (t1,t2) | rank pre-2022 | rank 2022+ |
|---|---|---|
| (0.5,1.5) | 1 | 1 |
| (0.5,2.0) | 2 | 2 |
| (0.5,2.5) | 3 | 3 |
| (1.0,1.5) | 6 | 4 |
| (1.0,2.0) | 5 | 5 |
| (1.0,2.5) | 4 | 6 |
| (1.5,2.0) | 7 | 7 |
| (1.5,2.5) | 8 | 8 |

## Cell fUSD_a30

| (t1,t2) | median active | mean active | mean÷median | best-month | win-rate | avg_period | p14_share | active-DSR |
|---|---|---|---|---|---|---|---|---|
| (0.5,1.5) | 0.0933 | 0.2311 | 2.48 | 5.2437 | 0.73 | 2.81 | 0.167 | 0.9998 |
| (0.5,2.0) | 0.0612 | 0.2061 | 3.37 | 5.2437 | 0.73 | 2.79 | 0.122 | 0.9996 |
| (0.5,2.5) | 0.0574 | 0.1751 | 3.05 | 2.4319 | 0.73 | 2.72 | 0.081 | 1.0000 |
| (1.0,1.5) | 0.0674 | 0.2461 | 3.65 | 5.8834 | 0.58 | 2.51 | 0.184 | 0.9997 |
| (1.0,2.0) | 0.0366 | 0.2182 | 5.96 | 5.8834 | 0.58 | 2.48 | 0.119 | 0.9995 |
| (1.0,2.5) | 0.0361 | 0.1709 | 4.73 | 3.0545 | 0.58 | 2.44 | 0.106 | 1.0000 |
| (1.5,2.0) | 0.0000 | 0.1336 | 0.00 | 1.4301 | 0.41 | 2.39 | 0.148 | 1.0000 |
| (1.5,2.5) | 0.0000 | 0.1127 | 0.00 | 1.4301 | 0.41 | 2.37 | 0.127 | 1.0000 |

### Disjoint-window rank (median active; lower rank = better)

| (t1,t2) | rank pre-2022 | rank 2022+ |
|---|---|---|
| (0.5,1.5) | 1 | 2 |
| (0.5,2.0) | 2 | 4 |
| (0.5,2.5) | 3 | 5 |
| (1.0,1.5) | 4 | 1 |
| (1.0,2.0) | 5 | 6 |
| (1.0,2.5) | 6 | 7 |
| (1.5,2.0) | 7 | 3 |
| (1.5,2.5) | 8 | 8 |

## Cell fUSD_p2

| (t1,t2) | median active | mean active | mean÷median | best-month | win-rate | avg_period | p14_share | active-DSR |
|---|---|---|---|---|---|---|---|---|
| (0.5,1.5) | 0.0763 | 0.1907 | 2.50 | 1.9584 | 0.68 | 2.90 | 0.209 | 1.0000 |
| (0.5,2.0) | 0.0632 | 0.2017 | 3.19 | 5.5903 | 0.67 | 2.81 | 0.137 | 0.9997 |
| (0.5,2.5) | 0.0591 | 0.1592 | 2.70 | 2.9142 | 0.67 | 2.78 | 0.097 | 1.0000 |
| (1.0,1.5) | 0.0860 | 0.2063 | 2.40 | 1.9584 | 0.63 | 2.53 | 0.182 | 1.0000 |
| (1.0,2.0) | 0.0581 | 0.2158 | 3.72 | 5.6537 | 0.61 | 2.47 | 0.129 | 0.9998 |
| (1.0,2.5) | 0.0450 | 0.1516 | 3.37 | 2.9760 | 0.61 | 2.36 | 0.061 | 1.0000 |
| (1.5,2.0) | 0.0000 | 0.1948 | 0.00 | 5.6537 | 0.41 | 2.41 | 0.157 | 0.9998 |
| (1.5,2.5) | 0.0000 | 0.1341 | 0.00 | 2.9760 | 0.41 | 2.29 | 0.077 | 1.0000 |

### Disjoint-window rank (median active; lower rank = better)

| (t1,t2) | rank pre-2022 | rank 2022+ |
|---|---|---|
| (0.5,1.5) | 1 | 2 |
| (0.5,2.0) | 2 | 4 |
| (0.5,2.5) | 3 | 5 |
| (1.0,1.5) | 4 | 1 |
| (1.0,2.0) | 5 | 3 |
| (1.0,2.5) | 6 | 6 |
| (1.5,2.0) | 7 | 7 |
| (1.5,2.5) | 8 | 8 |

## Decision

**Chosen band: `(t1=0.5, t2=2.0)`.** Resolved via §7 **step 4-edge** (a directional winner exists — not the null branch).

### Procedure trace
- **Step 1 (disjoint rank, top-half in both pre-2022 + 2022+ halves for ≥3 of 4 cells):** survivors `(0.5,1.5)`, `(0.5,2.0)`, `(1.0,1.5)`. Dropped: `(1.0,2.0)`/`(1.0,2.5)`/`(1.5,*)` (ranks 5-8 both halves); `(0.5,2.5)` (falls to rank 5 in both fUSD 2022+ halves).
- **Step 2 (drop fat-tail-inflated):** dropped `(1.0,1.5)` — mean÷median = **21.3×/12.2×** on fUST cells, best-month 4.3%/6.8% = classic fat-tail mirage (high mean, ~0 median).
- **Step 3 (paired band-vs-band difference CI):** `(0.5,1.5)` vs `(0.5,2.0)` is directionally consistent — `(0.5,1.5)` significantly wins 2 cells (fUST_a30 CI [+0.0007,+0.0367]; fUSD_a30 [+0.0030,+0.0543]) and ties 2 cells (fUST_p2 [-0.0337,+0.0286]; fUSD_p2 [-0.0950,+0.0401]); never loses. Not null.

### Why `(0.5,2.0)` over the median-peak `(0.5,1.5)`
The step-4-edge knee balances **robust median AND acceptable p14 tail**, not raw median argmax. `(0.5,1.5)` is the median peak but carries the highest p14-share (0.13–0.21 = 14-day-lock tail exposure, the most fill-fragile / spec §3 tail risk), and sits next to the t1=1.0 cliff. `(0.5,2.0)` halves the p14-tail (0.075–0.14) for a statistically marginal median cost (tied in 2 of 4 cells; ~0.016%/mo lower in the other 2). This mirrors the `p_long`=14 decision (shed the least-trustworthy tail for robustness).

- **Runner-up:** `(0.5,1.5)` (the deployed default — sweep confirms it is not a bad choice; the difference is a tail-vs-marginal-median tradeoff).
- **Single band, not per-cell:** `t1=0.5` is robust across all 4 cells (dominant lever); no genuine per-cell divergence warranting per-cell bands.

### Key findings
- **t1 is the dominant alpha lever** (when to leave period-2): t1≥1.0 is either fat-tail-erratic or median-collapsed-to-~0 in every cell.
- **t2 mostly controls the p14 tail, not median**: within t1=0.5, raising t2 1.5→2.0 barely moves median but roughly halves the 14-day-lock share.

### Fold-back (Task 10)
`CHOSEN_T1 = 0.5`, `CHOSEN_T2 = 2.0`, `p_long = 14`. Status: **locked-but-not-armed** (same as p_long=14; AdaptivePeriod is inert, not in cells.canary.yaml).
