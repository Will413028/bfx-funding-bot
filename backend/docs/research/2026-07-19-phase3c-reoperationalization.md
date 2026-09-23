# Phase 3c Signal Re-operationalization Funnel

**Run date**: 2026-07-19
**Data window**: 2016-07-31 .. 2026-07-19, 4 canary cells (fUST_a30, fUST_p2, fUSD_a30, fUSD_p2), daily-resampled
**Follow-up to**: [2026-06-06 signal-EDA-funnel ADR](../../../../wiki/projects/bfx-funding-bot/decisions/2026-06-06-signal-eda-funnel.md) — `frr_trend`/`spike_detect` KILLed (median IC -0.037 / -0.060, regime-fragile). ADR Followup: "若日後要做,需不同 horizon / operationalization 重新立論並先過 EDA 漏斗."

## Methodology (unchanged from the 2026-06-06 ADR — reused, not rewritten)

Same 4-gate framework, same tool: `src/bfx_funding_bot/modules/backtest/signal_eda.py`
(`spearman_ic`, `block_bootstrap_ic`, `quintile_spread`, `bh_fdr`, `decide_signal`,
`split_regime`, `add_forward_rate_change`, `resample_daily`, `build_signal_frame` —
all imported unmodified). GO requires: FDR-significant IC, same sign across both
disjoint regimes (split 2022-01-01), ≥3/4 cells robust, |median IC| ≥ 0.03.

**Data access**: read-only SELECTs pulled from the VM's self-hosted Postgres via a
one-off docker container (`docker compose -f docker-compose.bot.yml run --rm --no-deps
--label autoheal=false --entrypoint python ...`, same pattern as prior VM research runs)
using the already-deployed `bfx-bot:local` image (sha `b1cd87a`) — **no deploy, no
change to canary.env, no live/canary container touched**. The heavy compute (funnel +
bootstrap) ran locally against daily-resampled CSV snapshots (`build_signal_frame` +
`resample_daily`, reused unmodified) scp'd off the VM, so no DB session was held open
during the multi-minute run.

**Code changes**: 2 new pure functions added to `signal_eda.py` (`frr_curvature`,
`spike_pctile`), each with unit tests (TDD) — `frr_trend` and `spike_z` (existing
formulas) were reused verbatim, only the horizon range tested against them is new. No
change to `SIGNALS` (the production registry), `decide_signal`, `IC_FLOOR`,
`CELL_MAJORITY`, or any gate threshold.

## Proposed operationalizations

| # | Signal | Construction | Why it might catch what the original missed |
|---|---|---|---|
| 1 | `frr_curvature_k3` | Second discrete difference of FRR: `(frr_t - frr_{t-k}) - (frr_{t-k} - frr_{t-2k})`, normalized by rolling FRR std (k=3) | `frr_trend` (first-order momentum) was KILLed as regime-fragile; a sign-stable trend can still miss the *inflection point* where a move is decelerating/about to reverse. Curvature targets that turning-point moment directly instead of sustained drift. |
| 2 | `spike_pctile_w14` | Rolling percentile rank of FRR within its own trailing 14-day window (reuses the already-vetted `_rolling_pctile` helper behind `funding_supply`/`utilization`) | `spike_detect` (z-score) was KILLed. A z-score's own baseline window is inflated by the very spike it's trying to measure (heavy-tailed FRR), diluting its own signal. Percentile rank is order-based, not moment-based, so it can't self-dilute this way. |
| 3 | `frr_trend_extH` / `spike_z_extH` | **Same formulas as the 2026-06-06 originals** (k=3 / w=14), retested at horizons the ADR never covered: H ∈ {45, 60, 90} days (vs. the original {2, 7, 14, 30}) | The 2026-06-06 grid showed \|IC\| growing *monotonically* with H up to 30d in the one directionally-consistent bucket (fUST late regime, spike_detect: -0.050→-0.118→-0.172→-0.230 at H=2/7/14/30) while the early regime stayed same-signed but just under the FDR bar. Untested question: does that growth continue past 30d, and does a longer horizon finally buy the early regime enough power to clear significance? |

Full horizon set `{2,7,14,30,45,60,90}` was scored for the two brand-new constructs (#1,
#2); only the new horizons `{45,60,90}` were scored for #3 (retesting `{2,7,14,30}`
would just repeat the already-decided 2026-06-06 KILL). All 160 observations (4
signals × cells × regimes × their respective horizons) went through one shared
BH-FDR pass, matching the pooled-multiple-testing discipline of the original ADR.

## Primary funnel results (block_size=20, matching 2026-06-06 methodology)

| Signal | Verdict | Median IC | Reason |
| --- | --- | --- | --- |
| `frr_curvature_k3` | **KILL** | +0.001 | sign flips across regimes (regime-fragile) |
| `frr_trend_extH` | **KILL** | -0.102 | only 2/4 cells robust (< 3 majority) |
| `spike_pctile_w14` | **KILL** | -0.087 | only 2/4 cells robust (< 3 majority) |
| `spike_z_extH` | **GO** | -0.178 | 4/4 cells robust, median IC -0.178 |

52/160 observations FDR-significant. Full per-cell × regime × horizon grid in the
`.json` sidecar.

Notable patterns in the raw grid:
- `frr_curvature_k3`: genuinely weak and inconsistently signed (fUST early regime
  negative, late regime flips positive-then-negative) — a clean null, not a
  near-miss.
- `spike_pctile_w14`: fUST **late** regime is strong (IC -0.22 to -0.26, all
  FDR-significant, horizon-monotonic — nearly identical magnitude to `spike_z_extH`'s
  late-regime numbers) but the **early** regime never clears significance (same
  failure mode as the original `spike_detect`/`frr_trend` KILLs) → only 2/4 cells
  robust.
- `spike_z_extH`: negative IC in **every** cell/regime/horizon combination tested
  (24/24 same-signed), FDR-significant in 20/24; quintile spread negative in all 24
  as well (secondary metric, consistent with the primary IC).

## Robustness check: block-bootstrap block-size sensitivity at long horizons

**Concern**: `block_bootstrap_ic`'s `block_size=20` default (from the 2026-06-06 ADR)
was sized for the original H≤30d horizons ("~3-4x the typical ~5-day autocorrelation
length of daily funding rates"). At H=45/60/90 the forward-return target
(`mean(close in (t, t+H]) - close_t`) overlaps `(H-1)/H` between consecutive rows — a
much longer induced serial-correlation length than a 20-row block can capture. An
under-sized block risks an artificially tight bootstrap CI/p-value, i.e. a spurious
GO on exactly the long-horizon observations this round depends on.

**Check**: reran the identical funnel (same `decide_signal`, `bh_fdr`, same IC point
estimates) with `block_size=max(20, h)` — block length scaled to the horizon — for
`frr_trend_extH` and `spike_z_extH`.

| Signal | Verdict (block_size=20) | Verdict (block_size=max(20,h)) |
| --- | --- | --- |
| `frr_trend_extH` | KILL — only 2/4 cells robust | KILL — **0 cells robust** (early-regime significance evaporates entirely) |
| `spike_z_extH` | **GO** — 4/4 cells robust | **GO** — 4/4 cells robust (unchanged) |

The check is doing real work, not rubber-stamping: `frr_trend_extH`'s marginal 2/4
result depended on borderline early-regime p-values (e.g. fUST_a30 early H=60:
p=0.016 at block_size=20 vs. p=0.040 → not FDR-significant at block_size=60) that
don't survive the more conservative resampling — exactly the artifact this check was
designed to catch. `spike_z_extH`'s IC magnitudes are large enough (median -0.178,
range -0.08 to -0.26) that its significance survives the stricter test in the same
3-4 cells that were already strong under the primary run. This is meaningful
corroborating evidence that the `spike_z_extH` GO is not a bootstrap-block artifact.

## Final verdicts

| Signal | Verdict | Median IC | Basis |
| --- | --- | --- | --- |
| `frr_curvature_k3` | **KILL** | +0.001 | regime-fragile, both block sizes |
| `frr_trend_extH` | **KILL** | -0.102 | fails majority gate, fails harder under robustness check |
| `spike_pctile_w14` | **KILL** | -0.087 | fails majority gate (early regime underpowered), both block sizes |
| `spike_z_extH` | **GO** | -0.178 | 4/4 cells robust, survives block-size robustness check |

## Caveats on the `spike_z_extH` GO (read before any promotion decision)

- **Same lineage as the already-KILLed `spike_detect`.** This is not a new formula —
  it's the identical z-score construct from the 2026-06-06 ADR, just measured against
  a forward window the ADR never tested (45-90d vs. 2-30d). That KILL already showed
  the same sign and a monotonically growing \|IC\| up to H=30 in 3/4 cells; this result
  is the continuation of that trend past 30d, not an independent discovery.
- **Long-horizon target smoothing is a known confound.** `fwd_dH` averages over more
  days as H grows, which mechanically reduces target noise and tends to inflate \|IC\|
  for *any* signal correlated with the local rate level/trend — independent of whether
  the signal actually has 45-90 day predictive lead time. The growing IC with H is
  consistent with either "real long-horizon predictive power" or "smoother target,
  same underlying short-horizon effect measured with less noise." The EDA funnel as
  designed cannot distinguish these; only a proper OOS backtest at the strategy layer
  can.
- **Cell asymmetry.** fUST (a30, p2) is unambiguously strong (\|IC\| 0.14-0.26,
  FDR-significant almost everywhere). fUSD is weaker and less consistent, especially
  in the late regime (IC -0.04 to -0.12, only 2/3 horizons FDR-significant). The "4/4
  cells robust" pass is real per the pre-registered gate, but the effect is
  concentrated in fUST — which happens to be the currently-funded real-money currency,
  so this isn't disqualifying, just worth knowing going in.
- Per the project's engineering norm (`CLAUDE.md`) and this task's brief: **this GO is
  not being built into a strategy.** It is flagged as a candidate for Will's review;
  strategizing (engine plumbing + WFO/OOS) is a separate, later decision.

## Promotion

Signals to promote to full strategy (engine plumbing + WFO/OOS) — **pending Will's
review, not auto-promoted**:
- **`spike_z_extH`** (median IC -0.178) — spike z-score signal (w=14), forecast
  horizon 45-90 days, all 4 canary cells, both disjoint regimes. See caveats above
  before committing engine work.

`frr_curvature_k3`, `frr_trend_extH`, `spike_pctile_w14`: KILL. Recorded as falsified
alongside the 2026-06-06 originals.

## Suggested handling of the Phase 3c pending item

The project wiki's Phase 3c pending note (`wiki/projects/bfx-funding-bot/index.md`,
"FRR-trend / SpikeDetect... 需不同 horizon / operationalization 重新立論並先過 EDA
漏斗") should **not be closed as fully falsified** — for the first time since Phase 3c
was opened, one re-operationalization (`spike_z_extH`) cleared the pre-registered
4-gate bar including a block-size robustness check. Suggested update:

- Mark `frr_curvature` (2nd-derivative), `spike_pctile` (quantile-rank spike), and
  `frr_trend` retested at long horizons as **falsified** — added to the do-not-repeat
  list alongside the original `frr_trend`/`spike_detect`/`funding_supply`/`utilization`
  KILLs, so a future agent doesn't re-spend budget on the same dead ends.
- Change the pending status from "all falsified, needs new operationalization" to
  "one candidate (`spike_z_extH`, long-horizon spike z-score) passed EDA — awaiting
  Will's go/no-go on strategizing it (engine plumbing + WFO/OOS), given the
  target-smoothing and cell-asymmetry caveats above."
- If Will declines to strategize it (e.g. judges the long-horizon-smoothing confound
  too likely), downgrade `spike_z_extH` to falsified too and close Phase 3c's signal
  hypotheses entirely — at that point 6 distinct operationalizations across 2 rounds
  will have been screened.
