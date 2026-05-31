# G3 OOS Backtest Report — bot-vs-idle Headline Reframe

**Date**: 2026-05-31
**Status**: design approved
**Scope**: pure renderer + test change in `scripts/run_oos_profitability.py` and
`tests/scripts/test_run_oos_profitability.py`. **No new computation.** Mirrors the
framing already shipped for the live G3 report (Task 4, commits `47c62e2`/`6ef6f3e`,
`scripts/run_g3_live_validation.py::render_markdown`).

## Background

The OOS profitability report characterizes the deployed canary config
(MeanReversion × fUST × {a30, p2}) over rolling 1-month historical windows. It
currently leads its TL;DR with the strategy's absolute return **and** the
active-vs-passive metrics (median active, information ratio) side by side, treating
them as co-equal. This is the same framing defect the live G3 report just fixed:
active-vs-passive (MR timing alpha vs AlwaysMarketRate) is a near-zero, conditional
diagnostic that does **not** gate the product decision, whereas the bot's absolute
return on budget — which equals bot-vs-idle because an idle balance earns 0% — is the
unconditional product-value claim.

This reframe makes the two reports (live G3 + OOS backtest) speak one metric
vocabulary: **bot-vs-idle = primary/headline, MR timing alpha = secondary,
non-gating diagnostic.**

## Key facts (no new math)

- bot-vs-idle absolute return is already computed: `summarize_oos(strat_outcomes)`
  produces `s.median_monthly` / `s.annualized_pct` / `s.worst_monthly` /
  `s.idle_rate`. Because the idle arm is 0% by construction, the strategy's absolute
  monthly return **is** the bot-vs-idle edge — no new arm, no new calculation.
- The metrics being demoted (median active, information ratio, pct months
  outperform) come from `active_return_summary(strat_outcomes, base_outcomes)` →
  `r.active.*`. They stay in the report as a secondary diagnostic.
- The existing generated doc (`docs/research/2026-05-28-canary-oos-profitability.md`)
  predates the current `AlwaysMarketRate` baseline label and still renders the stale
  `AlwaysFRR` text — regenerating after the reframe also repairs this drift.

## Changes

### 1. Module docstring (`scripts/run_oos_profitability.py` line ~4)

Reframe the one-line description: primary metric is the bot's absolute monthly
return on budget (= bot-vs-idle, idle earns 0%); the AlwaysMarketRate comparison
(active return / information ratio) is a **secondary, non-gating MR-timing-alpha
diagnostic**. Keep the bootstrap-CI + selection-bias deflated-Sharpe mention.

### 2. TL;DR (`render_markdown`)

- Add a leading definition line: primary metric = bot-vs-idle; an idle balance earns
  0%, so the strategy's absolute return **is** the bot-vs-idle edge.
- Per-cell headline bullet keeps only the primary/robustness fields:
  **bot-vs-idle median %/mo + annualized % + worst-month % + idle rate +
  deflated-Sharpe.** Remove `median_active` and `information_ratio` from this bullet.
- Add a trailing note: MR timing alpha (vs AlwaysMarketRate) is a secondary
  diagnostic, near 0 means MR timing adds little over always-lending, and it does
  **not** gate the headline — see per-cell sections.

### 3. OOS honesty caveat

Add one bullet mirroring the live report's honesty caveat: a positive bot-vs-idle
backtest asserts the bot beats an idle balance (earns the market rate on budget),
**not** that MR timing beats always-lending — that is the separate MR-alpha
diagnostic.

### 4. Per-cell section

- Table baseline column already reads `Baseline (AlwaysMarketRate)` (kept). Relabel
  the strategy column header to `Strategy (= bot-vs-idle)` so the primary framing is
  explicit at the table.
- Relabel the bootstrap CI line to `bot-vs-idle median monthly 95% CI`.
- Rename section heading `### Active return vs passive` →
  `### MR timing alpha vs passive (secondary diagnostic)` and add a non-gating note:
  diagnostic only, does NOT gate the headline; near 0 means MR timing adds little
  over always-lending; the product value is bot-vs-idle.

### 5. JSON output

**No change.** `_report_to_json` stays a faithful serialization of the dataclass
fields; the `active` block keeps its raw keys. Framing belongs in the
human-readable markdown, not the machine data contract.

### 6. Regenerate the report doc

After the render change passes, regenerate
`docs/research/2026-05-28-canary-oos-profitability.md` (and its `.json` sibling) so
the committed doc equals the generator output and the stale `AlwaysFRR` label is
gone. This reads Neon historical candles only — offline, no canary/live exposure.

## Domain guardrails (do NOT regress)

- Strategy/baseline duration stays `p2 = 2 days` (the bot posts a 2-day offer).
  `funding_stats.avg_period` (~25 days) is the market auto-renew average, **not** the
  hold period — using it would fabricate ~12× interest.
- This is a pure framing change. No metric is recomputed; `median_monthly` /
  `annualized_pct` are already the idle≡0 absolute return.

## Testing

`tests/scripts/test_run_oos_profitability.py`:

- `test_render_markdown_contains_key_sections`: add assertions that the rendered
  markdown contains `bot-vs-idle`, `secondary diagnostic`, and `MR timing alpha`,
  alongside the existing `OOS honesty caveat`, `Non-backtestable risk register`,
  `Selection bias`, and the cell label.
- `test_render_markdown_handles_infinity_sortino_and_ir`: keep — IR `Infinity` still
  appears in the secondary diagnostic section, so the assertion holds.

Gate: `cd backend_py && uv run pytest -m "not integration"` green; `uv run mypy src/`
(scripts/ not in the mypy gate); `uv run ruff check`.

## Out of scope

- No change to `summarize_oos`, `active_return_summary`, or any backtest math.
- No change to the JSON schema/keys.
- No change to duration / period_agg semantics.
