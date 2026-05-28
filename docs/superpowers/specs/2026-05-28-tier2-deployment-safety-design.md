# Tier 2 Deployment Safety — Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-28
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-28
**Builds on**: `2026-05-28-canary-oos-profitability-validation-design.md` (OOS module + research doc)
**Closes**: the `canary-mr-config-inert` finding — deployed MR params do not beat the passive baseline

## Why

The canary OOS profitability run (`docs/research/2026-05-28-canary-oos-profitability.md`)
proved the **deployed** MeanReversion config is **inert**: on both fUST cells the active
return vs the passive AlwaysFRR baseline is identically `0` (information ratio 0, 0% of
months differ from passive, 0% idle). The strategy is running, submitting, earning — but
its selectivity adds nothing. We are paying for a MeanReversion engine and getting passive
FRR.

Root cause is **train/serve skew in the parameter-selection process**, in three layers:

1. **Hand-picked, not winner-selected.** `configs/cells.yaml` documents `threshold_sigma=1.0
   (middle of grid)` and `ema_alpha=0.01183 (→ ema_span=168)`. These were chosen by hand
   from the middle of the sweep grid, **not** taken from the WFO winner-picker
   (`pick_sweep_winner`, `matrix.py:30-49`). With `ema_span=168` the EDA-derived
   `ratio_sigma ≈ 0.99`, so the decision band `lower_band = −threshold_sigma × ratio_sigma
   ≈ −0.99` (`mean_reversion.py:50-57`) — the strategy only pauses if close is ~99% below
   its EMA, i.e. never. The evidence points to `ema_span=24, threshold_sigma=0.5` as the
   config that actually has an edge (recoverable alpha: +0.06–0.07%/mo active, 84–86% of
   months win).

2. **Numbers are hand-copied with a "MUST re-derive" warning, no enforcement.**
   `cells.yaml` carries the comment *"Any param change MUST re-derive ratio_sigma from
   current EDA"* — a manual ritual that depends on a human running `eda_phase3b.py`, reading
   stdout, and re-typing values. `run_oos_profitability.py:72-76` then **duplicates** the
   canary params into a hardcoded `CanaryCell` list labelled "single source of truth for
   deployed params" — a second hand-copy that can silently drift from the YAML it claims to
   mirror.

3. **Nothing validates that the deployed config is the validated config.** The OOS work
   characterized profitability but ran *after* deploy as a one-off; there is no gate that
   blocks a deploy/merge when the shipped config collapses to passive.

The unifying principle for the fix is **validate-exactly-what-you-deploy**. The cause of
every layer above is that the artifact that was reasoned about (a sweep, an EDA table, a
hardcoded mirror) was never the artifact that shipped. Tier 2 closes that gap with a
reproducible derivation pipeline and a deterministic gate that runs against the **same data
the params were derived from**.

This is a pre-launch refactor window (no paying users; canary is $450 real money). The
correct-but-larger fix is cheap now and expensive later.

## What

Three deliverables, unified into one reproducible pipeline plus one gate:

1. **Fixed-param selection (the actual fix).** Derive each MeanReversion cell's params from
   the WFO winner-picker, not by hand. Update `cells.yaml` + `cells.canary.yaml` to the
   selected winner (expected: fUST `ema_span=24, threshold_sigma=0.5`; fUSD MR cells
   re-derived in the same pass). The inert config is replaced by one with a measured edge.

2. **Deploy sanity gate (highest leverage).** A deterministic CI check: load a frozen candle
   fixture, run the *deployed* cells' backtest against AlwaysFRR over the rolling-OOS
   windows, and **fail** if the config is statistically indistinguishable from passive or is
   worse. This is the guardrail that would have caught the inert config before it shipped.

3. **Reproducible param-derivation pipeline.** `scripts/derive_cells.py` with `--write` and
   `--check` modes (analogous to `uv lock` / `uv lock --check`). `--write` pulls Neon once,
   freezes the fixture, runs EDA + WFO winner selection, and writes the params + a
   `_provenance` block into the YAML — no human transcription. `--check` re-derives from the
   committed fixture and asserts the committed YAML matches — run in CI, no Neon.

The end state: the inert config is gone, the new params are committed, and the gate is green
on the new config and would be red on the old one.

## Out of Scope (Tier 3 — deferred to scale-up)

- **Plateau / robustness selection** — picking params from a stable region of the grid
  rather than the single best point. Deferred; `pick_sweep_winner` (best point) is used.
- **Live tracking-error monitoring** — comparing live canary behavior against the backtest
  distribution at runtime. This is the *freshness-critical* check and belongs in monitoring,
  not a merge gate. Deferred.
- **Live config-lineage hashing** — tracking which config hash is live across deploys. The
  `_provenance.data_hash` in this design is a **build-time fixture-integrity hash only** (so
  `--check` is meaningful); it is not the live-lineage system.
- **RatePercentile param re-derivation** — RP is LOCF-disqualified (Phase 4.3) and not in
  the canary. The pipeline `--write` does not touch RP params (they are static `{percentile:
  75, lookback_hours: 168}`, not EDA-derived). `--check` still covers RP cells for drift.
- **Full CPCV / PBO / Monte Carlo** — already deferred in the OOS spec; unchanged.
- **fUSD deployment** — fUSD MR cells are re-derived into the fixture/YAML, but fUSD is not
  deployed until it is funded and per-currency allocation ships. The gate validates the file
  that actually deploys (canary = fUST MR).

## Methodology

### Parameter derivation (replaces the hand-picked middle-of-grid)

Per MeanReversion cell `(symbol, period_agg, timeframe)`:

```
candles      = fixture[symbol, period_agg, timeframe]          # frozen, post-2022
ratio_sigma  = close_over_ema_sigma(train_portion, ema_span)   # eda.py:49-64, per grid variant
winner       = pick_sweep_winner(run_cell_wfo(MeanReversion, candles, grid))  # matrix.py:30-49
```

The grid is the existing `param_grid_for_cell` (`mean_reversion.py:60-73`): `ema_span ∈
{24, 168} × threshold_sigma ∈ {0.5, 1.0, 1.5}`, with `ratio_sigma` from EDA at the
variant's `ema_span`. `pick_sweep_winner` filters `fill_rate ≥ 0.3 ∧ n_trades ≥ 10`, then
(because lending yields `Sortino = +∞` commonly) tie-breaks on max `net_monthly_return`.

**Determinism requirement.** The winner pick must be a pure function of the fixture. The
grid is enumerated in a fixed order and the tie-break is made stable by appending the param
tuple as a final sort key, so equal-metric variants resolve identically every run.

### Sanity gate (the deterministic deploy guard)

Per deployed cell, reuse the OOS rolling evaluation (`compute_wfo_windows` + per-window
`run_backtest` for strategy and `AlwaysFRRStrategy(period_days=2)`, identical to the OOS
spec) — but against the **fixture**, not Neon — producing `WindowOutcome`s. Then apply the
ratified two-part rule to `active_return_summary` + `bootstrap_ci` (both from
`oos_profitability.py`):

- **(a) Distinguishability** — the strategy must actually act differently from passive:
  `information_ratio ≠ 0` **and** `% months strat ≠ base > 0`. (The inert config fails here:
  IR = 0, 0% differ.)
- **(b) Not worse than passive** — the bootstrap 95% CI **lower bound** of mean active
  return `≥ 0`.

A cell **passes** iff (a) ∧ (b). The gate fails the build if any deployed cell fails. The
stricter "significantly better" bar (CI lower bound strictly `> 0`) is available as a
config flag but **off by default** — on a 49-window noisy sample it would reject genuine
but noisy edges (see Risks).

### Why a frozen fixture, not live Neon (recorded rationale)

A deploy gate must be a **pure function of the commit**. Wiring CI to live Neon would make
the same commit pass or fail depending on when it runs (candles append hourly; backfills and
corrections mutate history), break "green means mergeable", expose production credentials in
CI, and couple CI to serverless DB availability. Best practice for data-dependent validation
gates (quant/ML) is a **pinned, version-controlled dataset**; live-data comparison is a
monitoring concern (Tier 3), not a merge gate. The 4-year window is append-only history that
does not change, so the fixture only needs refreshing when params are deliberately
re-derived — which produces a new fixture anyway. Crucially, **the fixture is produced by the
derivation pipeline**, so it is by construction the exact data the params were validated on:
validate-exactly-what-you-deploy holds end-to-end and is reproducible in CI.

## Architecture

### Schema change: store `ema_span`, drop `ema_alpha`

Cells currently store `ema_alpha: 0.01183` and the loader converts `ema_span = round(2/α −
1)` (`strategy_registry.py:42-58`). The grid, the strategy, and the derivation all work in
`ema_span` natively; `ema_alpha` is a derived-then-rounded artifact that is itself a skew
source. Change the YAML schema to store `ema_span: int` directly and remove the conversion.

- Touches: `CellConfig` (params validation), `strategy_registry.build_strategy`,
  `marketfeed/config.py` cell parsing, both YAML files, `run_oos_profitability.py`'s
  `alpha_to_ema_span` usage.
- RatePercentile cells are unaffected (no ema params).
- Contained, pre-launch; removes a lossy round-trip and a hidden skew source.

### New: frozen candle fixture

- `backend_py/fixtures/candles/<symbol>_<period_agg>_<timeframe>.parquet` — one file per
  series needed by any derived/deployed cell: fUST `{a30, p2}`, fUSD `{a30, p2, p30}`,
  timeframe `1h`, post-2022.
- Written by `derive_cells.py --write` from a single Neon pull (`get_candles_in_range`).
- Format: parquet (compact, typed). **Size to be measured during implementation**; if the
  committed size is unacceptable, fall back to git-lfs (decision recorded in the plan, not
  pre-judged here).
- A content hash over all fixture files is recorded in `_provenance.data_hash`.

### New: `_provenance` block in `cells.yaml`

Embedded in the deployment YAML (not a separate lockfile — keeps the deploy artifact single
and self-contained):

```yaml
_provenance:
  data_hash: <sha256 over fixture files>
  derived_at: 2026-05-28T...Z
  fixture_window: {start: 2022-01-01, end: <run date>}
  cells:
    mean_reversion/fUST/a30: {ema_span: 24, threshold_sigma: 0.5, ratio_sigma: <eda>, net_monthly: ..., fill_rate: ...}
    ...
```

`cells.canary.yaml` does not carry its own `_provenance`; its invariant is "params identical
to `cells.yaml` for shared keys" (already its stated contract), now machine-checked by
`--check`.

### New: `scripts/derive_cells.py`

Thin orchestration over existing primitives; two modes:

- `--write` (human, deliberate, needs Neon): pull candles → write fixture → for each MR cell
  run EDA (`close_over_ema_sigma`) + WFO sweep (`run_cell_wfo` + `pick_sweep_winner`) → patch
  the MR `params` in `cells.yaml` and `cells.canary.yaml` + write `_provenance`. RP cells and
  non-derived fields (`reference_amount_usdt`, `staleness_budget_hours`) are preserved
  (merge, not clobber).
- `--check` (CI, no Neon): load committed fixture → assert `_provenance.data_hash` matches
  the on-disk fixture → re-derive MR params from the fixture → assert they equal the
  committed YAML params → assert `cells.canary.yaml` params match `cells.yaml` for shared
  keys. Any mismatch exits non-zero. (If re-deriving the full sweep proves too slow for
  every-push CI, fall back to comparing committed YAML against `_provenance` and running the
  full re-derive on a less frequent schedule — decision deferred to the plan after measuring
  sweep time on the fixture.)

### New: sanity gate

- A pytest test (offline, deterministic, reads the committed fixture) under a dedicated
  marker (e.g. `gate`) so it runs as its **own CI job**, keeping `pytest -m "not
  integration"` fast.
- Reuses `oos_profitability.py` evaluation + `active_return_summary` + `bootstrap_ci`,
  reads the deployed cells from `cells.canary.yaml`, and applies rule (a) ∧ (b).
- A small pure function `evaluate_gate(strat, base, *, strict=False) -> GateResult` holds
  the pass/fail logic (unit-testable in isolation from any fixture/DB).

### Refactor: `run_oos_profitability.py`

- Remove the hardcoded `CanaryCell` mirror (`:72-76`); read cells from `cells.canary.yaml`
  via the existing loader. Kills the second hand-copy.
- Accept a candle source switch (Neon | fixture) so the OOS research doc and the gate share
  one evaluation path. Default stays Neon for the research script.

### Reused, unchanged

`modules/backtest/{wfo,engine,oos_profitability,sortino,eda}.py`,
`strategies/{mean_reversion,always_frr}.py`, `modules/candles/repository.py`,
`marketfeed/strategy_registry.build_strategy`, `matrix.pick_sweep_winner`.

## Data flow (end to end)

```
1. derive_cells.py --write   (human, once, needs Neon)
   Neon → fixtures/*.parquet → EDA + WFO winner → cells.yaml + cells.canary.yaml params + _provenance
2. git commit                (fixture + YAMLs + provenance together, one reviewable PR)
3. CI on push                (no Neon)
   ├─ derive_cells.py --check   → committed YAML == re-derive(fixture)?      [drift / hand-copy]
   └─ pytest -m gate            → deployed cells beat AlwaysFRR on fixture?  [significance]
4. manual deploy script      (unchanged; reads the validated cells.canary.yaml)
```

## Testing (TDD)

- **`evaluate_gate`** (pure): synthetic `WindowOutcome`s → inert (active ≡ 0) fails (a);
  worse-than-passive fails (b); good config passes; noisy-but-positive passes under default
  (CI lower bound ≥ 0) and fails under `strict=True`.
- **`derive_cells` derivation** (pure, on a tiny synthetic fixture): deterministic winner +
  `ratio_sigma`; stable tie-break (equal-metric variants resolve identically across runs).
- **`derive_cells --check` drift detection**: mutating one committed param → non-zero exit;
  mutating the fixture without updating `data_hash` → non-zero exit; canary/full param
  divergence → non-zero exit.
- **Schema migration**: `ema_span` is read and built correctly; existing cell behavior is
  byte-for-byte unchanged for an equivalent span (regression guard on `build_strategy`).
- **Gate end-to-end** (offline, `gate` marker): runs the committed fixture + deployed YAML
  and passes on the new config; an explicit regression asserts the **old** inert params
  (span=168/thr=1.0) would fail the gate.
- Commit gate stays `pytest -m "not integration"` + mypy --strict + ruff; the `gate` job and
  `--check` run as additional CI steps. `derive_cells --write` is integration/manual (needs
  Neon), never in the no-network gate.

## Implementation Order

1. Schema change `ema_alpha → ema_span` + loader/migration tests (smallest, unblocks the
   rest; YAMLs temporarily keep current params, just re-expressed as span).
2. `evaluate_gate` pure function + unit tests (the rule, independent of data).
3. `derive_cells.py` derivation core + unit tests on a synthetic fixture (winner + EDA,
   deterministic).
4. `--write`: Neon pull → fixture freeze → patch YAMLs + `_provenance`. Run it once →
   produces the new winner params + fixture. **Inspect the selected winner** (sanity: is it
   the expected span=24/thr=0.5? are fUSD cells sane?).
5. `--check` + drift tests.
6. Sanity gate test + the old-config-fails regression; wire `gate` job and `--check` into
   `.github/workflows/ci.yml`.
7. Refactor `run_oos_profitability.py` to read the YAML + fixture; re-run the OOS research
   doc against the new config to confirm the recovered edge.
8. Full gate green on new config; commit; decide merge of `feat/oos-profitability-validation`.

## Risks & Open Questions

1. **Fixture size in git.** 4 years hourly × 5 series may be a few MB (committable) or larger
   if more columns are needed. Measured in step 4; git-lfs fallback documented, not
   pre-judged.
2. **`--check` cost in CI.** Re-deriving the full WFO sweep on every push may be slow. Step
   5 measures sweep time on the fixture; if too slow, downgrade per-push `--check` to a
   `_provenance`-vs-YAML comparison and schedule full re-derive less often.
3. **Small-sample noise (49 windows).** The default gate rule is deliberately the (a)+(b)
   floor, not strict-better, to avoid rejecting genuine-but-noisy edges. The gate proves the
   config is *not passive and not worse* — not that it is significantly profitable. True OOS
   remains the live canary; the gate is a regression guard against re-shipping an inert
   config.
4. **Winner may still be near-passive.** If even the WFO winner barely beats passive, the
   gate will surface it (it may fail (a) or sit at the (b) boundary). That is a finding to
   bring to Will, not a pipeline bug — the carry edge may simply be thin, and the decision
   becomes whether MeanReversion is worth running over plain AlwaysFRR at this size.
5. **Platform/credit tail uncapturable.** Unchanged from the OOS spec — the binding
   catastrophic risk is mitigated by the $450 cap, not by any gate here.
