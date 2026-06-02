# Adaptive-Period — `p_long` Sensitivity Sweep + Deploy Plan

**Date**: 2026-06-03
**Question**: How much of adaptive period's edge is the robust median duration effect vs the fill-fragile fat-tail spike-capture, as a function of the max lock `p_long`? Pick the deploy `p_long`.
**Method**: re-ran the OOS harness (`run_oos_profitability.py --cells`) for `p_long ∈ {7, 14, 30}` (with `p_mid=7, t1=0.5, t2=1.5, ema_span=24` fixed), both windows (recent 2022→ 50 windows/cell; full history fUST 86 / fUSD 115). Configs: `cells.experimental-p7.yaml`, `-p14.yaml`, `cells.experimental.yaml` (p30). Full-history via temporary `START_MTS=2016` override (reverted, uncommitted). All `n_trials=4`.
**Fill model**: linear, mean fill = 1.0 (the optimism caveat the fat tail leans on).

## Decision

**Deploy with `p_long = 14`.** It is the knee of the curve: it retains ~80–84% of the robust *median* duration edge while shedding roughly half of the fill-fragile *fat tail* that `p_long=30` adds, and halves the 30-day duration/credit tail-risk. `p_long=7` sacrifices too much median (down to ~51% of p30 on fUSD) for the extra fill-safety; `p_long=30`'s headline advantage over p14 is almost entirely fat-tail that live will not reproduce.

## Evidence (full history — the regime where the fat tail lives)

`median active` = robust, fill-realistic duration edge (vs always-2d). `mean active` = fat-tail-inclusive. `mean/median` = how fat-tailed the gain is. All %/mo.

| Cell | median p7→p14→p30 | mean p7→p14→p30 | mean/median @p30 | p14 median as % of p30 |
|---|---|---|---|---|
| fUST_a30 | 0.077 → 0.093 → 0.112 | 0.097 → 0.142 → 0.246 | 2.2× | 83% |
| fUST_p2  | 0.097 → 0.098 → 0.106 | 0.128 → 0.134 → 0.252 | 2.4× | 92% |
| fUSD_a30 | 0.057 → 0.093 → 0.112 | 0.127 → 0.231 → 0.438 | 3.9× | 83% |
| fUSD_p2  | 0.047 → 0.076 → 0.097 | 0.122 → 0.191 → 0.405 | 4.2× | 78% |

- **p14→p30 adds little median (+~20%) but a lot of mean (+~90%)** → the extra is fat tail, not robust edge. Example fUSD_a30: median 0.093→0.112 (+20%) vs mean 0.231→0.438 (+90%).
- **best-month (fat-tail proxy) collapses with shorter cap** for the dominant cell: fUSD_a30 full best-month **14.76% (p30) → 8.34% (p14) → 5.53% (p7)**. (best-month is noisy/non-monotonic on the thin fUST_p2/fUSD_p2 cells — rely on mean active for the fat-tail read.)
- **win rate is ~flat across p_long** (71–77% full history) → the *consistency* of beating always-2d does not depend on the cap; longer caps just add lumpier upside. p14's edge is real, just less fat-tailed.

### bot-vs-idle annualized (headline, for reference — fat-tail-inflated at p30)

| Cell | recent p7/p14/p30 | full p7/p14/p30 |
|---|---|---|
| fUST_a30 | 7.79 / 8.62 / 9.94 | 7.98 / 8.57 / 9.91 |
| fUST_p2  | 7.54 / 8.20 / 9.38 | 8.44 / 8.52 / 10.05 |
| fUSD_a30 | 7.56 / 9.18 / 11.94 | 11.29 / 12.65 / 15.38 |
| fUSD_p2  | 6.94 / 8.21 / 10.56 | 11.05 / 11.97 / 14.81 |

Even at p14, bot-vs-idle (8.5–12.7% full / 7.5–9.2% recent) still beats MeanReversion (7.7–10.7% full / 6.7–7.2% recent) and AlwaysMarketRate (6.7–9.6% / 5.6–6.2%) in every cell. The duration edge survives the cap; we are only trading away the least-trustworthy tail.

## Caveats (unchanged from the characterization)

100%-fill is most violated exactly in the fat tail we're cutting → p14 is also the more *honest* live estimate, not just the safer one. Params still in-sample-ish (EDA from 2022-2026; tiers chosen, not selected by a gated sweep). 14-day locks still carry more platform/credit tail-duration than 2-day, bounded by caps.

## Deploy 串接 plan (gated — NOT built yet)

Going live is **double-gated**; nothing below is armed today:

1. **Gate A — live MR G3 verdict** (≥8 weekly windows, ~2 months out). Proves the live execution path + bot-vs-idle thesis on real money before adding a second strategy. Until A clears, adaptive_period stays experimental.
2. **Gate B — adaptive_period's own shadow/parity validation.** Because variable period is new behavior and the edge is fill-fragile, it needs its own live==replay parity + a shadow period before real money.

When the gates approach, the wiring (in order) is:
- **B1. `divergence_reporter` period-boundary handling** — `period_days` is a step function of the tolerance-compared EMA, so tier-boundary EMA drift can flip period (2↔7↔14) and trip a false divergence ([[g2-state-level-divergence]] class). Add an adaptive_period branch to `_strategy_attributes`/`_normalize_signal_score` that tolerance-compares period when the underlying deviation is within `rel_tol` of `band1`/`band2`. Pure code + tests; the one piece that's correctness-required regardless of gate outcome.
- **B2. derive_cells handling** — adaptive_period tiers are *chosen*, not swept-and-selected, so it does NOT fit the MR distinguish/not-worse `derive_cells` flow. Either hand-set params with a dedicated fixture check, or exclude from the MR drift gate explicitly. Decide at wiring time (the MR-specific `check_against_fixture` already ignores non-MR cells, so the minimal path is: hand-set + a small parity test).
- **B3. canary cell** — add one `adaptive_period` cell (`p_long=14`) to `cells.canary.yaml` behind the per-currency caps/balance gate, shadow first, then small real-money.

**Recommendation on timing**: build B1–B3 when Gate A nears clearing, not now. Rationale: (a) it's blocked for ~2 months; (b) the divergence/fill design genuinely benefits from G3's live-fill learnings (how big is real EMA drift? how partial are real fills? — that directly tunes B1's tolerance and recalibrates how much of even the p14 tail to trust); (c) building live-parity code that then sits untested for 2 months is bitrot-prone. The high-value, low-bitrot decision — **which `p_long` to deploy — is locked now (14)**. The deploy candidate config is recorded; the wiring is specified above and sequenced behind the gates.
