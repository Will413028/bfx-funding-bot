# Adaptive-Period — Band `(t1, t2)` Sensitivity Sweep — Design Spec

**Date**: 2026-06-04
**Status**: design (brainstormed, pre-plan)
**Author**: Will + Claude
**Predecessors**: [[2026-06-03-adaptive-period-strategy-design]] (the strategy),
`docs/research/2026-06-03-adaptive-period-plong-sweep.md` (the `p_long` sweep this mirrors)

---

## 1. Context & Motivation

`AdaptivePeriodStrategy` lends at `candle.close` every cycle and selects `period_days`
from the rate's deviation-from-EMA via two band edges:

```
deviation = (candle.close - EMA) / EMA
band1 = t1 * ratio_sigma     # deviation > band1  -> period = p_mid (7)
band2 = t2 * ratio_sigma     # deviation > band2  -> period = p_long (14)
                             # deviation <= band1 -> period = 2 (floor)
```

`p_long` has been swept (`{7,14,30}` → **14**, the knee that keeps ~80–84 % of the robust
median edge while shedding ~half the fill-fragile fat tail). But the band edges
`(t1, t2)` — which decide *when* the rate climbs each period tier — have **never been
systematically compared**. The `p_long` sweep held them fixed at `(0.5, 1.5)`; the
strategy's `param_grid_for_cell` lists only two pairs `{(0.5,1.5),(1.0,2.0)}` and was
never run through a gated WFO (AdaptivePeriod was skipped in Phase 3b). So the deployed
band candidate `(0.5, 1.5)` is an untested default.

`period` is the strategy's only alpha lever; `p_long` set the *ceiling* of that lever,
and `(t1, t2)` set *how readily it is pulled*. This sweep closes the one remaining
unexamined degree of freedom before the strategy is armed (B3, gated behind live G3).

### 1.1 Why now (low-bitrot, pre-launch)

The `p_long` sweep's timing logic applies verbatim: the high-value, **low-bitrot,
offline** decision (which band to deploy) can be locked now; only the live-parity wiring
(B1–B3) should wait for G3's live-fill learnings. Band selection is pure backtest
characterization — it does not bitrot — and the pre-launch window is the free time to
get the parameter right.

### 1.2 What the prior round killed (carried-in lesson)

A `fill-sensitivity` sweep was brainstormed first and **discarded** after an adversarial
design review + live-data check proved: (a) a pure candle-derived sweep cannot answer
"is live≈0 a fill artifact?" (circular — the break-even is the inverse of the same
curve), and (b) the bot places at-market and fills near-instantly (`offers_avg=0.02` in
prod `reconcile_observation`), so "offers not filling" is not the live-≈0 mechanism. The
load-bearing lesson for **this** spec: **do not let a backtest sweep claim to answer a
question that requires a live anchor.** This sweep makes only the relative,
backtest-internal claim it is entitled to (§3).

---

## 2. Goals & Non-Goals

### 2.1 Goals (measurable — each is a ship/no-ship criterion)

- **G1.** For each of the 4 canary cells (fUST/fUSD × {a30, p2}), produce
  `median active`, `mean active`, `mean÷median`, `best-month`, `win-rate`, and
  `bot-vs-idle annualized` for **all 8** `(t1, t2)` band variants, over **both** the
  recent (2022→) and full-history windows — one apples-to-apples table per cell, same
  columns as the `p_long` sweep report.
- **G2.** Produce a **disjoint-window rank-stability** read per cell: each band's rank by
  `median active` on **2016–2021** (full minus recent) vs **2022→** (recent) — two
  *non-overlapping* halves, so a band that only works in the post-2022 regime is exposed
  (the nested recent⊂full comparison cannot do this — see §6 defense 2).
- **G3.** Emit a **deflated-Sharpe** per band, `n_trials = 8`, computed on the per-window
  **active** series (`paired_active_returns` vs always-2d) — the quantity band selection
  optimizes — NOT the band-invariant bot-vs-idle level (which saturates ~1.0 for every
  band). Degenerate tight bands (active variance → 0) are reported as "near-baseline, DSR
  undefined," not as a pass.
- **G4.** Deliver a **decision** reproducible from the tables by the §7 procedure —
  including the explicit option **"bands are statistically indistinguishable (paired
  difference CI straddles 0) → keep the band with the least p14-tier tail-risk, do not
  treat band as a tuned lever"** (null result is a first-class outcome, not a failure).
- **G5.** Fold the chosen `(t1, t2)` **and** `p_long=14` back into
  `param_grid_for_cell` (currently hardcoded `p_long=30`, `(t1,t2)` v1 pair), retiring
  the `p_long` sweep's loose end. Zero change to live behavior (AdaptivePeriod is inert).

### 2.2 Non-Goals (YAGNI)

- **No WFO / gated selection.** At this N (8 bands × ~86–115 monthly windows, median
  gaps ~0.05–0.11 %/mo « month-to-month noise) a walk-forward optimizer is *false rigor*:
  each fold's parameter pick is itself noise. It would also diverge from the MR / `p_long`
  characterization route for a single parameter. (Spectrum + rationale in §6.)
- **No tier sweep** (`p_mid`, `p_long` stay 7 / 14). This sweep is band-edges only.
- **No `ema_span` sweep** (fixed 24, matching `p_long` sweep and canary MR).
- **No claim about live≈0, no break-even-vs-idle, no fill modeling** (§1.2, §3).
- **No change to `cells.canary.yaml`, `derive_cells`, `divergence_reporter`, or any live
  path.** Those are B1–B3, gated behind live G3 (deferred, designed in the strategy spec §7).
- **No engine / `oos_eval` algorithm change.** Strategy already runs in the harness.

---

## 3. Core Framing (what this sweep is and is NOT entitled to claim)

**Entitled claim (relative, backtest-internal):** *"Among 8 band positions, holding rate
at market and `p_long=14` fixed, band X gives the most robust median duration edge over
always-2d with the least fat-tail, consistently across both regimes."* This is a legal
in-sample-ish comparison — same kind the `p_long` sweep and MR characterization already make.

**NOT entitled:** any statement that the chosen band will reproduce its backtest edge
live, that it resolves the live-≈0 question, or that its `median active` is significantly
different from its neighbours unless the §6 robustness checks support it. All numbers
share the `p_long` sweep's standing caveats: 100 %-fill optimism, EDA `ratio_sigma` from
2022–2026, tiers chosen-not-gated. The deliverable is **locked-but-not-armed**, identical
in status to `p_long=14`.

---

## 4. Mechanism (mirrors the `p_long` sweep)

Reuse the existing OOS harness end-to-end — **zero changes to `engine.py` /
`oos_eval.py`**. The strategy already runs in `evaluate_oos_windows` because it implements
the `Strategy` ABC and the engine already honours variable `period_days`.

Two viable plumbing options; **recommend (B)**:

- **(A) Mirror `p_long` exactly** — hand-write 8 `configs/cells.experimental-band-*.yaml`
  files (each: 4 cells, `ratio_sigma` borrowed per-cell from `cells.experimental-p14.yaml`,
  only `t1`/`t2` differ), run `run_oos_profitability.py --cells <f>` 16 times (8 × 2
  windows), collate by hand. Zero new code; tedious, error-prone, 8 hand-authored YAMLs.
- **(B, recommended) A thin sweep driver** — `scripts/run_adaptive_band_sweep.py` that
  programmatically enumerates the 8 `(t1, t2)` pairs, builds each `CellConfig` in-memory
  (borrowing `ratio_sigma` per cell), calls the **existing** `evaluate_oos_windows` (no
  reimplementation of OOS logic), and renders one comparison table per cell + the
  cross-window rank table. Adds a `--start-mts` passthrough so full-history no longer needs
  the uncommitted `START_MTS=2016` edit-and-revert hack. Small, testable, reproducible,
  guarantees grid consistency.

The driver is a **thin wrapper**: the OOS engine, `active_return_summary`, `bootstrap_ci`,
and `deflated_sharpe` primitives are reused unchanged. But two pieces of **new stats
wiring** sit on top (correcting an earlier draft's "zero new math" claim) — the shared
`build_cell_report` computes neither, and both are load-bearing for §6/§7:
1. **DSR on the active series** — feed `paired_active_returns(strat, base)` (not the
   bot-vs-idle level) into `sharpe_skew_kurt → deflated_sharpe(n_trials=8)` (§6 defense 3).
2. **Paired band-vs-band difference bootstrap** — per shared month, `A_active_m −
   B_active_m`, then `bootstrap_ci` of its mean/median (§6 defense 4 / §7 tie test).
Both compose existing primitives on a new input series; no new estimator is written.

**Output**: `docs/research/2026-06-04-adaptive-period-band-sweep.md` (+ per-cell JSON,
mirroring the `p_long` sweep artifacts).

---

## 5. Parameter Space

```
t1 ∈ {0.5, 1.0, 1.5}   (when to leave period-2 → p_mid)
t2 ∈ {1.5, 2.0, 2.5}   (when to enter p_long=14)
constraint: t1 < t2 (strict; else the p_mid band vanishes)

→ 8 variants:
   (0.5,1.5)*  (0.5,2.0)  (0.5,2.5)
   (1.0,1.5)   (1.0,2.0)  (1.0,2.5)
   (1.5,2.0)   (1.5,2.5)
   * = current deployed candidate (in-grid, so its relative position is visible)
```

Fixed across all variants: `ema_span=24`, `p_mid=7`, `p_long=14`, `ratio_sigma` =
per-cell EDA value (strategy-independent; reuse the four values already in
`cells.experimental-p14.yaml`).

**The grid is 2-D, not a single monotone line** — and the two axes carry *different* risks:
- `t1` (the p2↔p7 boundary) drives **avg_period** — it moves a large share of candles, so
  it dominates "how far from always-2d."
- `t2` (the p7↔p14=p_long boundary) drives the **p14-tier share** — the 14-day
  lock/credit tail-risk §3 names as the actual reason to prefer a "safe" band.

These are orthogonal: a low-`t1`/high-`t2` band can be "looser" on avg_period yet carry
*less* p14 tail. So "tightest = safest" is false off-diagonal. Every per-cell table must
surface **both** avg_period and p14-share per band, and §7's safety tie-break keys on
**p14-share (t2)**, not avg_period.

---

## 6. Selection-Bias / Overfit Defense

**Spectrum (industry):** in-sample argmax (never) → train/test split → **WFO** (Pardo's
systematic-trading standard) → **CPCV** (López de Prado). Orthogonal overfit-risk tools:
**Deflated Sharpe** (penalize "tried N configs"), **PBO** (prob. OOS < median),
**plateau-over-peak** (pick a stable region, not a fragile spike).

**Decision for this N: robustness over performance.** WFO/CPCV are false rigor here —
the per-fold pick is noise (§2.2). Instead, four honest defenses (all computed per cell):

1. **Plateau, not argmax (per axis).** The grid is 2-D (§5), so "neighbours" are taken
   **along each axis separately**: prefer a band whose `median active` is stable as `t1`
   and as `t2` each move one notch — not an isolated peak (peaks sit next to cliffs =
   fragile).
2. **Disjoint-window replication (corrected primary robustness gate).** Score each band on
   **2016–2021** and **2022→** as *non-overlapping* halves; require top-half in **both
   truly-independent** halves. The earlier nested design (recent ⊂ full) was
   auto-correlated — a sample vs its own superset shares ~50 windows, so rank agreement was
   tautological and blind to exactly the post-2022 regime overfit that `ratio_sigma` (a
   2022–2026 EDA artifact, §3) is most prone to. Note the smaller per-half N (fUST
   ~36 / ~50, fUSD ~65 / ~50) and widen expected rank noise accordingly. Full-history
   headline numbers may still be reported, but **labelled in-sample-overlapping**, not a
   generalization check.
3. **Deflated Sharpe on the ACTIVE series, `n_trials=8`.** Computed on
   `paired_active_returns(strat, always-2d)` — the statistic band selection actually
   optimizes — via `sharpe_skew_kurt → deflated_sharpe`. The reused `build_cell_report`
   DSR is on the band-invariant bot-vs-idle *level* (saturates ~1.0 across all bands — the
   p7/p14/p30 sweep already shows 0.9994–1.0) and is shown as **non-gating context only**.
   Degenerate tight bands (active variance → 0) report "near-baseline, DSR undefined."
   Caveat retained: `n_trials=8` deflates band-selection only (OQ1).
4. **Paired band-vs-band difference test (the tie / null detector).** Two bands run the
   SAME test months vs the SAME always-2d baseline, so each band's *marginal* CI reflects
   common market noise, not the band-to-band difference — a marginal-overlap test is
   mechanically biased to always declare "tied," burying a real period edge. Instead, per
   shared month compute `A_active_m − B_active_m`, `bootstrap_ci` its mean/median, and call
   the pair indistinguishable **iff that difference CI straddles 0**. This is the
   variance-correct test and the trigger for the null branch. (`mean÷median` and
   `best-month` remain the fat-tail-honesty read, as in the `p_long` sweep.)

**Null result is first-class (G4).** If, after defenses 1–3, the surviving bands are
pairwise indistinguishable by defense 4 (difference CIs straddle 0) — the *expected*
outcome at this N — the correct conclusion is: **band is not a meaningful lever; keep the
band with the least p14-tier tail-risk (highest `t2`) and stop sweeping it** — analogous to
the fill-sweep being judged off-target. The design must manufacture neither a false
champion **nor** (the symmetric failure the corrected defense-4 prevents) a false tie.

---

## 7. Decision Procedure (reproducible from the tables)

Apply in order, per the §6 defenses. **"Majority of cells" = ≥3 of 4**; a 2-2 split is
**no majority** and falls through to the paired test (step 3) then the safety tie-break.

1. **Drop window-unstable bands** — any band not in the top half of `median active` rank in
   **both disjoint halves** (2016–2021 and 2022→) for ≥3 cells (defense 2).
2. **Drop fat-tail-inflated bands** — high `mean÷median` / `best-month` outliers vs peers.
3. **Paired indistinguishability test** — among survivors, run the §6-defense-4 paired
   band-vs-band difference bootstrap. If every surviving pair's difference CI straddles 0
   (or cells split 2-2 with no paired winner) → **null result**, go to step 4-null.
4. **Resolve:**
   - **4-null (tie) →** pick the band with the **least p14-tier tail-risk = highest `t2`**
     (lowest p14 share; NOT lowest avg_period — those disagree on the 2-D grid, §5),
     document "band not a meaningful lever."
   - **4-edge (a band wins) →** pick the plateau-centre knee among survivors (defense 1,
     per-axis stable): robust median edge, acceptable p14 tail, stable neighbours on both
     axes. Record the runner-up and the paired-difference margin.

**Per-cell vs single band:** the default deliverable is **one** `(t1,t2)` robust across
cells. `param_grid_for_cell` is keyed by `(symbol, period_agg)`, so per-cell bands are
*technically* permissible — but adopt them only if step 3's paired test confirms a
**genuine** per-cell divergence (not 2-2 noise); otherwise the single tightest-band
fallback wins and §8 folds back one band. Either way the outcome is locked-but-not-armed.

---

## 8. param_grid_for_cell Fold-Back (G5)

After the decision, update `AdaptivePeriodStrategy.param_grid_for_cell`
(`adaptive_period.py:104-125`):

- Set `p_long = 14` (retire the hardcoded `30` + "v1" comment — supersedes the `p_long`
  sweep's loose end).
- Collapse the `(t1, t2)` list to the chosen band (single band; or the short plateau if
  step 4-edge kept one).
- **ema_span=168 row:** the sweep tests **span-24 only** (§5), so it has *zero* evidence
  on the span-168 grid row. Do **not** let span-24 results silently re-author it. Resolve
  one of two ways (decide at impl, *lean (a)*): **(a)** drop the span-168 row entirely — AP
  runs span-24 in every experimental/canary cell and the WFO `param_grid` path is unused
  for AP (it goes through cells-config), so the grid should represent the *deployed
  candidate*, not an untested span; **(b)** keep span-168 at its current values and fold
  back only the span-24 row, documenting the asymmetry. State the choice in the docstring.
- Update the docstring to cite this sweep + the `p_long` sweep as provenance.

This is a pure-characterization metadata change: AP is inert (not in `cells.canary.yaml`),
so it has **zero** live-behavior impact (the deploy-gate and divergence tests read
`cells.experimental-p14.yaml` + fixtures, not the grid).

**Test impact — the grid IS read by a test.** `test_param_grid_for_cell_from_eda`
(`test_adaptive_period.py:201-213`) asserts `len(grid)==4`,
`{(t1,t2)}=={(0.5,1.5),(1.0,2.0)}`, `ema_spans=={24,168}`, and `p_long`. The fold-back
makes it go red. §12 must therefore **UPDATE** (not merely add) this test to the new
`p_long=14` + chosen-band + resolved-span expectations, or §11's "pytest fully green" fails.

---

## 9. Open Questions (do not block the plan; resolve during impl)

- **OQ1.** `n_trials` honesty: is 8 (band-grid only) the right deflation, or should it
  compound the prior `p_long`(3) and `ema_span` choices? *Lean*: report DSR at
  `n_trials=8` for band selection + a one-line note that cumulative selection is covered
  by cross-window consistency, not DSR. Decide final wording at impl.
- **OQ2.** Full-history start: make `--start-mts` a first-class driver arg (recommended)
  vs document the temporary override. *Lean*: first-class arg (kills the revert hack).
- **OQ3.** Rank-consistency threshold (G2, now on **disjoint** halves per §6 defense 2):
  "top half in both halves" vs a stricter Spearman across the 8 bands. *Lean*: start with
  top-half (interpretable); add Spearman only if ambiguous. Either way use the disjoint
  2016–2021 / 2022→ split, **never** the nested recent⊂full one.
- **OQ4.** Driver (B) vs hand-YAML (A): confirm (B) at plan time; if (B)'s test surface
  feels heavier than the sweep warrants, fall back to (A).

## 10. Risks & Failure Modes

- **R1 — Manufactured champion OR false tie (symmetric).** Reader treats an in-noise
  `median active` peak as a real edge; **or** a pairing-blind test mechanically declares
  everything tied and buries a real period edge. *Mitigation*: §6 defenses (esp. the
  *paired* difference test, defense 4, which is variance-correct in both directions) + §7
  null branch are mandatory, not optional. The original marginal-CI test failed toward
  false-tie; the paired test is the fix.
- **R2 — Cross-cell disagreement.** Different bands win different cells (likely, given
  per-cell `ratio_sigma`). *Mitigation*: decision procedure operates on "majority of
  cells"; if cells genuinely disagree, report per-cell bands rather than forcing one — but
  flag the added overfit surface.
- **R3 — Scope creep into WFO/live wiring.** *Mitigation*: §2.2 hard non-goals; this PR
  touches only the driver + report + `param_grid` fold-back + tests.
- **R4 — DSR misread as fill-robustness** (the prior review's trap). *Mitigation*: §6
  caveat; DSR is about selection deflation only, reported once per band at its own scale.
- **R5 — `ratio_sigma` mismatch.** Borrowing the p14 cell sigmas but changing bands is
  correct (sigma is strategy-independent EDA), but a copy error would silently shift all
  bands. *Mitigation*: driver reads sigma from one source of truth; a test asserts the 4
  values match `cells.experimental-p14.yaml`.

## 11. Acceptance Criteria

- [ ] Driver runs all 8 bands × 4 cells × 2 windows; per-cell comparison tables + JSON
      emitted to `docs/research/2026-06-04-...` (G1). Each table surfaces **both**
      avg_period and p14-share per band (§5).
- [ ] **Disjoint-window** (2016–2021 vs 2022→) rank table present (G2). Deflated-Sharpe
      `n_trials=8` on the **active** series per band; bot-vs-idle level DSR shown only as
      non-gating context (G3).
- [ ] Paired band-vs-band difference bootstrap implemented + reported for surviving bands
      (§6 defense 4 / §7 step 3).
- [ ] Report contains an explicit decision via the §7 procedure, **including** an executed
      null branch (paired difference CIs straddle 0) and the p14-share tie-break (G4).
- [ ] `param_grid_for_cell` updated to `p_long=14` + chosen band + resolved span-168 row;
      existing `test_param_grid_for_cell_from_eda` **UPDATED** (not just a new guard) (G5/§8).
- [ ] `cd backend_py && uv run pytest -m "not integration"` fully green; `mypy`/`ruff`
      clean. No diff to `cells.canary.yaml`, `engine.py`, `oos_eval.py`, live paths.
- [ ] Report's standing caveats (100%-fill, in-sample-ish, locked-but-not-armed) present
      verbatim-equivalent to the `p_long` sweep.

## 12. Testing Plan (TDD)

- **Driver unit tests** (new code):
  - grid enumeration yields exactly the 8 `(t1,t2)` pairs with `t1<t2` (no `(1.5,1.5)`).
  - per-cell `ratio_sigma` matches `cells.experimental-p14.yaml` (R5 guard).
  - `--start-mts` passthrough changes the window set; the disjoint halves (2016–2021 /
    2022→) are non-overlapping (OQ2 / §6 defense 2).
  - a band variant with a known tiny synthetic series produces the expected period-tier
    distribution (band edges wired correctly into `CellConfig`).
  - **active-series DSR**: `deflated_sharpe` is fed `paired_active_returns` with
    `n_trials=8` (NOT the bot-vs-idle level); a degenerate active-variance→0 band returns
    the "DSR undefined" sentinel (§6 defense 3).
  - **paired difference test**: a synthetic pair where band A strictly dominates B every
    shared month → difference CI excludes 0 (not tied); identical bands → straddles 0
    (§6 defense 4).
- **Fold-back tests**:
  - **UPDATE** `test_param_grid_for_cell_from_eda` to the new `p_long=14` + chosen-band +
    resolved-span expectations (§8 — it goes red otherwise).
  - add a guard mirroring `test_p14_config_present` that fails loudly if the grid silently
    reverts band / `p_long`.
- **No engine/oos_eval tests change** (no behavior change there).

## 13. References

- `docs/research/2026-06-03-adaptive-period-plong-sweep.md` — the sweep this mirrors
  (method, columns, decision style, caveats).
- `docs/superpowers/specs/2026-06-03-adaptive-period-strategy-design.md` — strategy §5
  (param_grid), §6 (harness reuse, config plumbing), §7 (deferred live wiring).
- `src/bfx_funding_bot/modules/backtest/strategies/adaptive_period.py:104-125` —
  `param_grid_for_cell` (fold-back target).
- `scripts/run_oos_profitability.py` — `--cells`, metrics, DSR, bootstrap (reused).
- `configs/cells.experimental-p14.yaml` — `ratio_sigma` source of truth (4 cells).
- Memory: `[[adaptive-period-strategy]]`, `[[best-practice-decision-protocol]]`,
  `[[g3-passive-baseline-frr-bug]]` (OOS framing), `[[spec-review-impl-status-pollution]]`.
