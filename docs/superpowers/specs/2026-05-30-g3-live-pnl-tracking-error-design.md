# G3 — Live P&L Tracking-Error / Active-vs-Passive Validation — Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-30
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-30
**Closes**: Phase 4.4 remaining「G3 P&L tracking error gate」/ deferred Tier-3「live tracking-error」(see 2026-05-28 OOS spec §Out of Scope, wiki line 247)

## Why

The 2026-05-28 OOS validation produced two results that frame G3:

1. It proved the **previously deployed** MeanReversion config (ema_span=168,
   threshold_sigma=1.0) was **mathematically equivalent to passive AlwaysFRR**
   (active return ≡ 0, zero alpha) — because `ratio_sigma` scales with `ema_span`,
   so the lower band sat at ≈ −0.99 and never triggered. The config was a hand-picked
   "middle of grid" point the WFO never validated (train/serve skew).
2. It showed the **alpha is recoverable**: a fixed (ema_span=24, threshold_sigma=0.5)
   config backtests to **+0.06–0.07%/mo active return, 84–86% of months beating passive,
   relative uplift +12.7%/+14.1%**. Tier-2 deployment safety shipped this winner to the
   canary on 2026-05-28 (deployment `f5ef37df`).

What we have: a **backtest** claim that the deployed config has alpha. What we do
**not** have: confirmation that this holds on **real fills**. The backtest is in-sample
to the parameter-selection sweep (same 2022–2026 history). The only true out-of-sample
test is the **live canary itself**. G3 closes that loop: before scaling real money beyond
the canary, verify the deployed config produces the predicted active spread on live data.

The strategy class is **yield / carry**. We validate it the way the OOS pass established
(wiki Lessons Learned 2026-05-28): on **active return vs the passive AlwaysFRR benchmark**,
**utilization / idle rate**, **worst-period**, downside-only Sortino — NOT directional
drawdown (a category error for lending; equity is monotonic). G3 reuses that exact
yield-native metric set, fed from live data instead of backtest windows.

## What

A **live validation report** (run on demand, like the OOS report) that characterizes the
realized active-vs-passive performance of the exact deployed canary config from live
`event_log` fills, benchmarked against a reconstructed AlwaysFRR counterfactual, with a
four-state human-read verdict. It is **not** an always-on monitor and does **not**
automate any trading or scaling decision — scaling stays a manual operator decision.

The canary is currently near-idle (realized 550 / available 19.10, signals skipping). So
**sparse-to-absent live data is the expected state**; the design tolerates it by emitting
a robust since-inception headline from the first fill and gating the distributional
statistics behind a minimum window count.

## The core problem this design solves

G3 hits the **same wall** that killed the "aggregate realized P&L from order_fill events"
plan for the L2 loss guards (wiki line 144): `OrderFilled.size_usdt` and `ledger._realized`
are **principal exposure, not profit**. There is no realized-interest P&L stream in the
system today. `fill_rate` (= `foc.rate`, the daily funding rate at match) is the only
yield signal in the event.

Three candidate sources for "live active yield" were weighed; best practice for **strategy
attribution on a small sample** is to isolate the alpha signal using the **same yield model
as the backtest**, then **reconcile it against an independent truth source** — attribution
for the signal, reconciliation for trust (what a quant shop does: risk system computes
expected P&L, reconciled daily against the broker's cash ledger; divergence triggers
investigation, not blind trust).

| Source | Measures | Decision |
|---|---|---|
| **A. Rate-spread attribution** (`event_log` fills + `funding_stats` FRR) | committed yield = deployed-rate × utilization, in the backtest's own `rate×period` units | **Primary gate signal.** Apples-to-apples vs the backtest being validated; isolates strategy alpha; pure-readable from durable data; needs no new infra. |
| **B. NAV-delta** (`ReconcileNavTracker`, in-memory) | true account-equity change incl. received interest + idle drag + everything | **Truth anchor (best-effort), not the gate.** On a near-idle canary NAV barely moves and deposits/withdrawals confound it. |
| **C. Funding-ledger ingestion** (`/v2/auth/r/ledgers`, not ingested) | authoritative received interest | **Deferred.** New external integration; marginal rigor does not change a $450→scale sizing decision. B already provides the anchor from data we already fetch. |

## Methodology

### Capital normalization

Both arms are normalized to a **fixed capital budget C = the canary allocation cap**
(`BFX_ALLOCATION_CAP_USDT`, currently 570). This is the mandate size — the standard
attribution baseline. It sidesteps the fact that live idle balance (`available`) is **not
persisted as a time series** (only in-memory; `position_state` persists reserved/realized
only). Utilization = deployed / C; idle drag = (C − deployed) / C earning 0 vs FRR.

### Active arm — yield attribution, per window W (length T days)

```
active_interest(W) = Σ over fills i in W:  size_i × rate_i × duration_i
  size_i     = OrderFilled.size_usdt
  rate_i     = OrderFilled.fill_rate          # daily funding rate at match
  duration_i = min(cell_period_days, release_ts_i − fill_ts_i)
                 cell_period_days: p2 → 2.0 ; a30 → funding_stats.avg_period (auto, ≤30)
                 capped by actual lifetime when a RESERVATION_RELEASED exists for the offer
active_return_pct(W) = active_interest(W) / C × 100
```

Held-to-term is the default (consistent with the backtest engine, which computes
`equity *= 1 + net_rate × period`). The release-event cap corrects early
cancel/expiry using events we **do** have (`RESERVATION_RELEASED`). Credit *maturity* has
no discrete event (no `fcc` parse; realized decrements only via reconcile) — so the
configured period is the held-to-term proxy for matured credits.

### Passive arm — AlwaysFRR counterfactual, per window W

```
passive_interest(W) = C × Σ over days d in W:  FRR_d        # FRR_d = daily FRR from funding_stats
passive_return_pct(W) = passive_interest(W) / C × 100
```

AlwaysFRR re-lends the full budget at FRR continuously — the naïve auto-renew baseline.
Same 15% fee applied to both arms (apples-to-apples), matching the OOS convention.

### Active spread

`active_spread(W) = active_return_pct(W) − passive_return_pct(W)`, per window. The
idle-drag downside is encoded automatically: a strategy that sits out deploys fewer
capital-days than the full-budget passive arm, so its return falls below passive and the
spread goes negative — exactly the dominant lending downside the OOS spec named.

Each (window, arm) → one `WindowOutcome(month_mts=window_start, net_monthly=return_pct,
n_trades=fills_in_window, fill_rate=mean_rate)`, feeding the **existing**
`oos_profitability` functions unchanged.

### Windowing — dual-tier (tolerates sparsity by design)

1. **Headline (since-inception aggregate):** one active-spread number + deployment stats
   over all live data. Available from the first fill; most robust; the operator's primary
   read.
2. **Weekly windowed distribution:** week-bucketed `WindowOutcome`s feed
   `active_return_summary` (IR, % months outperform), `bootstrap_ci`, Sortino. Weekly
   chosen because the p2 cell is a 2-day cycle — a week contains multiple full cycles while
   still accumulating several windows within a month. These distributional statistics emit
   only when **N ≥ 8 windows**; below that they report `INSUFFICIENT_DATA`.

## Truth anchors (reconciliation-for-trust)

- **Deployment anchor (durable, always checkable):** the attribution's assumed deployed-
  principal trajectory vs the venue-observed `realized` at reconcile checkpoints (persisted
  in `position_state` + `reconcile_observation`). If they diverge beyond tolerance, the
  attribution miscounts utilization → verdict `UNRELIABLE`. Validates the *deployment* half
  of the attribution against venue truth.
- **NAV / interest anchor (best-effort):** when process uptime spans the window,
  `ΔNAV ≈ Σ active_interest` (NAV from `ReconcileNavTracker` in-memory samples). When uptime
  is insufficient, the report states "NAV anchor unavailable" and the verdict degrades to
  PASS-with-caveat rather than asserting full reconciliation. **Durable NAV-sample
  persistence is a deferred follow-up** (it would also fix the `ReconcileNavTracker`
  restart-amnesia limitation noted in the #4 NAV-source work) — kept out of G3 v1 to avoid
  perturbing the live reconcile hot path with a migration + write.

## Verdict logic (four states, human-read)

- **PASS** — active-spread bootstrap CI lower bound > 0 (statistically beats passive)
  AND deployment anchor within tolerance AND (when available) NAV anchor within tolerance.
  Thresholds anchored to the backtest active return, NOT a copied point estimate as a hard
  bar.
- **INSUFFICIENT_DATA** — N < 8 weekly windows OR since-inception deployed capital-days
  below a minimum floor (default `Σ size_i × duration_i < C × 7` — less than one
  full-budget-week of cumulative deployment). Emits the headline; withholds the verdict.
  **Expected state for a long time** given the idle canary.
- **FAIL** — active-spread CI upper bound < 0 (significantly *loses* to passive — the live
  confirmation of an inert config).
- **UNRELIABLE** — any anchor diverges beyond tolerance (the yield model does not match
  reality; the comparison cannot be trusted).

The verdict + numbers are written to a report for the operator to read and decide on
scale-up. G3 takes no automated action.

## Architecture

### Reused modules (no changes)

- `modules/backtest/oos_profitability.py` — `summarize_oos`, `active_return_summary`,
  `paired_active_returns`, `bootstrap_ci`, `sharpe_skew_kurt`, `deflated_sharpe`,
  `percentile`, `WindowOutcome`, `OosSummary`, `ActiveReturnSummary`.
- `modules/funding_stats/repository.py` — FRR / avg_period time series.
- Event read: `event_log` via a query over `RESERVATION_CLAIMED` / `ORDER_FILL` /
  `RESERVATION_RELEASED` rows (see `pg_event_log_query.py` for the pattern).

### New module: `modules/live_validation/live_attribution.py`

Pure, no I/O, fully unit-testable. Operates on already-loaded inputs (lists of fill/release
records, FRR series, reconcile checkpoints) → metrics + verdict.

- `@dataclass(frozen=True) FillRecord` — `venue_offer_id`, `fill_ts_ms`, `size_usdt`,
  `rate`, `cell_key` (strategy/symbol/period_agg), `release_ts_ms: int | None`.
- `@dataclass(frozen=True) FrrPoint` — `mts`, `frr`, `avg_period`.
- `cell_period_days(cell_key, frr_avg_period) -> Decimal` — p2 → 2.0; a30 → avg_period.
- `attribute_active(fills, frr_series, *, capital, window_bounds) -> list[WindowOutcome]`
  — per-window active arm.
- `attribute_passive(frr_series, *, capital, window_bounds) -> list[WindowOutcome]`
  — per-window AlwaysFRR arm.
- `weekly_window_bounds(start_ms, end_ms) -> list[tuple[int,int]]` — calendar-week bins.
- `@dataclass(frozen=True) DeploymentAnchorResult` — attributed vs observed deployed
  principal, divergence, within_tolerance.
- `check_deployment_anchor(attributed_series, reconcile_checkpoints, *, tol) -> ...`.
- `@dataclass(frozen=True) NavAnchorResult` — available / unavailable, ΔNAV vs Σinterest,
  within_tolerance.
- `check_nav_anchor(nav_samples, total_attributed_interest, *, tol) -> ...`.
- `@dataclass(frozen=True) G3Verdict` — state ∈ {PASS, INSUFFICIENT_DATA, FAIL,
  UNRELIABLE}, headline_active_spread, n_windows, ci, anchors, reasons.
- `decide_verdict(headline, windowed_summary, ci, deployment_anchor, nav_anchor, *,
  min_windows=8, min_capital_days=C*7) -> G3Verdict` — pure decision table.

Starting tolerances (calibrated in the plan, all parameters not magic constants):
deployment anchor `tol = 5%` relative divergence (attributed vs venue realized);
NAV anchor `tol = 10%` relative (looser — NAV is confounded by deposits/fees timing).

### New script: `scripts/run_g3_live_validation.py`

Thin orchestration only:

- Loads fills (`event_log`), FRR series (`funding_stats`), reconcile checkpoints, and
  best-effort NAV samples from Neon, scoped to the canary account + `deployment_environment`.
- Reads `C` and cell config from `cells.canary.yaml` + `BFX_ALLOCATION_CAP_USDT`.
- Builds windows, calls the pure module, computes the existing OOS metrics + verdict.
- Writes `docs/research/<date>-g3-live-validation.md` + `.json`.

## Output Format

`docs/research/<date>-g3-live-validation.md`:

```
# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5
Run date / live data window / N weekly windows / total fills / verdict
## TL;DR — verdict + headline active spread (since inception) + deploy ratio
## Data coverage  (window, fills, deployed capital-days, idle rate, mean fill rate)
## Active vs passive  (since-inception spread; weekly distribution: median/CI, IR,
                       % weeks outperform, Sortino — OR INSUFFICIENT_DATA)
## Anchors  (deployment anchor: attributed vs venue realized; NAV anchor: ΔNAV vs Σinterest
             or "unavailable")
## vs backtest  (live active spread vs OOS-predicted +0.06–0.07%/mo)
## Honesty caveats  (held-to-term assumption; in-sample-to-selection params; platform/
                     credit tail uncapturable)
## Recommendation  (scale-up readiness; if data insufficient, what to wait for)
```

Plus a `.json` sibling for machine consumption.

## Testing

TDD on the pure module (no DB):

- `attribute_active`: synthetic fills + FRR with known values → exact active_return_pct;
  duration cap with/without a release event; multiple fills per window; zero-fill window.
- `attribute_passive`: known FRR series → exact passive_return_pct.
- `cell_period_days`: p2 → 2.0; a30 → avg_period passthrough.
- `weekly_window_bounds`: known span → expected week bins; partial trailing week.
- `decide_verdict`: each of the four states at its boundary (CI lo just >0 → PASS;
  N=7 → INSUFFICIENT_DATA, N=8 → eligible; CI hi <0 → FAIL; anchor divergence → UNRELIABLE).
- `check_deployment_anchor` / `check_nav_anchor`: within / beyond tolerance; NAV
  unavailable path.
- Reuse-integration: feeding produced `WindowOutcome`s through the existing
  `active_return_summary` / `bootstrap_ci` yields consistent results.

Script-level: integration smoke against Neon emits both files (not in the
`not integration` gate). Commit gate stays `pytest -m "not integration"` + mypy + ruff.

## Implementation Order

1. `live_attribution.py` pure functions + dataclasses + unit tests (TDD): attribution math,
   windowing, anchors, verdict decision table.
2. `run_g3_live_validation.py` script wiring (event_log + funding_stats + checkpoints loads).
3. Run against Neon → write the live validation report.
4. Present numbers to Will → scale-up decision.

## Risks & Open Questions

1. **Sparse / absent live data.** The canary is idle; there may be zero fills until a credit
   matures and reopens the gap. G3 must return a clean `INSUFFICIENT_DATA` headline, not
   error, on empty/thin data. Acquiring testable alpha may take weeks of live accrual.
2. **Held-to-term assumption.** Matured credits have no discrete close event, so the
   configured period is the duration proxy. Early cancels/expiries are corrected via
   `RESERVATION_RELEASED`; matured-credit over/under-attribution is bounded by the
   deployment anchor and stated as a caveat.
3. **a30 period is variable** (auto, FRR-matched). Using `funding_stats.avg_period` as the
   duration is an approximation; the deployment anchor catches gross mismatch.
4. **Selection bias persists.** Deployed params come from the same 2022–2026 sweep. The live
   canary IS the true OOS test — that is the point of G3 — but the sample is tiny, so CIs
   will be wide and the report must not over-claim.
5. **Platform / credit tail uncapturable.** Bitfinex insolvency / Tether risk is the real
   catastrophic risk and no attribution addresses it — mitigated by the cap, stated in the
   risk caveats so G3 is not mistaken for safety it cannot provide.
6. **NAV anchor durability.** v1 anchor is best-effort over in-memory samples; a drawdown or
   accrual spanning a restart is forgotten. Durable nav-sample persistence is the documented
   follow-up.

## Out of Scope

- **fUSD cells** — canary is fUST-only; per-currency allocation not yet shipped.
- **Funding-ledger ingestion (source C)** — deferred; NAV anchor covers trust from data we
  already fetch.
- **Durable NAV-sample persistence** — deferred follow-up (also fixes #4 restart amnesia).
- **Always-on monitor / automated scaling** — G3 is an on-demand report; scaling is a manual
  operator decision.
- **Tier-3 institutional machinery** (full CPCV/PBO, Monte Carlo path resampling, capacity
  analysis) — deferred to material AUM, per the 2026-05-28 OOS spec.
