# Research Infra C / D / E Implementation Plan

**Goal:** Give the strategy research loop the two things it cannot currently do — score
execution and tenor differences (fill model that discriminates, period-aware pricing) — and
then automate weekly re-validation on top of them.

**Source:** `backend_py/docs/research/2026-09-22-strategy-system-review.md` §5–§7. Will accepted
the order D → C → E on 2026-09-22 (D first: zero live risk, answers the biggest unknown; C's
snapshot cadence change rides the next deploy; E assembles once C and D scripts exist).

**Tech Stack:** Python 3.13, Decimal, SQLAlchemy 2.0 async, pytest; VM one-shot research
container for DB-backed runs.

## Why

- Every deployed strategy posts 2-day offers; FRR has traded at 1.7–3× the p2 close since
  2022. The tenor axis has never been scored correctly: the engine credited a30-series rates to
  2-day locks (review F2).
- The linear fill model returns fill=1 whenever quote=close, so no execution change is
  distinguishable in backtest and AlwaysFRR is judged dead-on-arrival (review F3).
- `/strategy-research` is manual; external-signal tables stopped in July; champion drift is
  unwatched (review §7).

## Progress

| Task | Status | Evidence |
|---|---|---|
| D1 engine: `market_series_by_agg` + `truncate_at_window_end` + `pricing_series_used` | ✅ 2026-09-22 | `test_engine_period_pricing.py` |
| D2 `period_structure.py`: hourly grid, arms, report | ✅ 2026-09-22 | `test_period_structure.py` |
| D3 runner `scripts/run_period_structure_backtest.py` (fixtures + DB) | ✅ 2026-09-22 | `tests/scripts/test_run_period_structure_backtest.py` |
| D4 registry pre-registration (4 arms, DSR 34→38) | ✅ 2026-09-22 | registry + `DEFAULT_N_TRIALS` |
| D5 fixture run (fUSD full, fUST without p30/FRR) + report | ✅ 2026-09-22 | `backend_py/docs/research/2026-09-22-period-structure-fixtures{,-fill1e-9}.md` — always_30d +0.47%/mo over always_2d (fUSD, CI [0.34, 0.61] both bounds); mr_a30 legacy inflation +0.08/+0.13%/mo; mr_a30 vs always_2d straddles 0 at alpha 5 → fill-dependent |
| D6 VM run with p30 + funding_stats (all arms, full history) | ⏳ operator | same |
| C0 `BFX_BOOK_SNAPSHOT_INTERVAL_S=300` in `deploy/vm/live.env` | ✅ 2026-09-22 | rides next release |
| C1 book-replay learner → `FillModelArtifact(source="book")` | ✅ 2026-09-22 | `book_replay.py` + `test_book_replay.py`; `scripts/learn_book_fill_rate.py` + sqlite test |
| C2 engine accepts `EMPIRICAL_SOURCES = {candle, book}`; `fill_models_by_agg` per-series scoring; unknown source still rejected | ✅ 2026-09-22 | `test_engine_fill_models_by_agg.py`, `test_engine_empirical_fill.py` (eligibility untouched: it never hard-coded the source) |
| C3 runner `--fill-model book` (DB only) + DEGENERATE note; unmodelled arms reported not scored | ✅ 2026-09-22 | `test_period_structure.py::test_book_models_score_each_tenor…`; VM run pending (needs `learn_book_fill_rate` first) |
| C4 calibration vs live fills (`report_execution_quality` latencies) | ⏳ needs live fills | |
| E1 `diff_research_report.py` + `research_diff.py` (drift / overtake rules; per-window data added to both JSON sidecars) | ✅ 2026-09-22 | `test_research_diff.py` |
| E2 compose `weekly-report` chain: ops first, then perp/liquidations topup → book learn → period-structure (book + linear) → OOS → two diffs, each fail-soft; systemd budget 1800→5400s | ✅ code 2026-09-22 | VM dry run pending (next release picks up compose + unit) |
| E3 registry rules pre-registered (champion p25 breach, challenger 3-week CI) | ✅ 2026-09-22 | registry「Weekly re-validation rules」段 |

## D — period-aware four-arm backtest

**Semantics decided here (not in the review):**

- Fill pricing picks the market series by tenor: `p2` for 2-day, `p30` for 30-day, `a30` for
  everything else (stated approximation; the report prints which series priced each arm).
- `truncate_at_window_end=True` for this runner only: a lock opened late in a test month is
  credited up to month end. Default stays off so deploy-gate / derivation numbers are byte-stable.
- p30 is LOCF'd onto the hourly grid (12h budget, same as `cells.yaml`) so the engine's
  index-based cooldown stays in wall time; slots beyond budget are `close=None`.
- All series are clamped to joint coverage before windows are computed.
- Arms: `always_2d`, `always_30d`, `adaptive_period` (ADR-locked t 0.5/2.0, p 7/14),
  `mr_a30` (period-aware), `mr_a30_legacy` (old pricing, diagnostic), `mr_p2`, `always_frr`
  (DB only).
- Double bound on the fill assumption: run with `--fill-alpha 5.0` (default) and `1e-9`
  (always fill). If a pair's CI straddles 0 under both, the answer is "wait for live" (§8 of
  the methodology page), not another parameter.

**Acceptance:** report shows `mr_a30_legacy_vs_mr_a30` > 0 (the mismatch was inflating a30),
`always_30d_vs_always_2d` with CI under both bounds, and the AP tiers priced as documented.

## C — book-replay fill model

Queue-position replay over `funding_book_snapshots` (see review §5). Blocked on nothing for
hourly data; 5-minute data accrues after the next release. Contract change: `FillModelArtifact`
scope gains `source="book"`; book and candle artifacts never mix (tech page Case 11).

## E — weekly re-validation job

Extend `docker-compose.bot.yml` `weekly-report`: existing G3 chain first, then external-signal
topup, fill artifact refresh, OOS re-run for registry DEPLOYED / NOT-PROMOTED rows,
week-over-week diff. Research steps never block the operational report. Never auto-promotes.

## Rollback

All D/C code is research-only (no live path). C0 is one env line; revert to hourly by removing
it. E is a compose profile change; the old command chain is in git.
