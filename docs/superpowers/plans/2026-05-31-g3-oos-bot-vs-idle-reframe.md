# G3 OOS Report bot-vs-idle Reframe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reframe the OOS profitability report's headline from co-equal strategy/active metrics to bot-vs-idle primary + MR-timing-alpha secondary diagnostic, matching the live G3 report.

**Architecture:** Pure-renderer change in `scripts/run_oos_profitability.py::render_markdown` plus its module docstring. No backtest math, no JSON schema, no duration semantics change. Tests assert the new framing strings in the rendered markdown. Final step regenerates the committed research doc from Neon historical candles (offline).

**Tech Stack:** Python 3.13 (run via `uv` from `backend_py/`), pytest. All commands run from `backend_py/`.

---

## Context for the implementer (read first)

- **Working dir:** all `pytest`/`uv`/`ruff` commands run from `backend_py/`. From repo root they hit the wrong Python (pyenv 3.12) and break on sqlalchemy.
- **The renderer is `render_markdown(reports, *, data_window)`** in `scripts/run_oos_profitability.py` (lines ~98–168 at plan time). It takes a `list[CellReport]` and returns a markdown string. It is pure and unit-testable without a DB — the tests build `CellReport` fixtures directly.
- **The data is already computed.** `CellReport.strat_summary` (an `OosSummary`) holds `.median_monthly`, `.annualized_pct`, `.worst_monthly`, `.idle_rate`. Because the idle arm is 0% by construction, `strat_summary.median_monthly` **is** the bot-vs-idle return. `CellReport.active` (an `ActiveReturnSummary`) holds `.median_active`, `.mean_active`, `.information_ratio`, `.pct_months_outperform` — these are the metrics being demoted to the secondary diagnostic.
- **Reference framing** lives in the just-shipped live report
  `scripts/run_g3_live_validation.py::render_markdown` — same project, same vocabulary. Match its phrasing ("secondary diagnostic", "does NOT gate", "idle arm = 0% by construction").
- **Domain trap — do NOT touch:** strategy/baseline duration stays `p2 = 2 days`. Do not introduce `funding_stats.avg_period`. This plan changes prose only; no metric is recomputed.
- **mypy gate:** `scripts/` is NOT in the mypy gate, so the renderer edit does not need mypy. The commit gate is `uv run pytest -m "not integration"` + `uv run ruff check`.

---

### Task 1: Reframe the renderer (TDD)

**Files:**
- Modify: `scripts/run_oos_profitability.py` — module docstring (line ~4) and `render_markdown` (lines ~98–168)
- Test: `tests/scripts/test_run_oos_profitability.py` — `test_render_markdown_contains_key_sections` (lines ~53–81)

- [ ] **Step 1: Strengthen the failing test**

In `tests/scripts/test_run_oos_profitability.py`, extend the assertions at the end of
`test_render_markdown_contains_key_sections` (after the existing `assert "Selection bias" in md`).
Add these assertions for the new framing:

```python
    # bot-vs-idle reframe: headline is the primary metric, MR alpha is secondary
    assert "bot-vs-idle" in md
    assert "MR timing alpha" in md
    assert "secondary diagnostic" in md
    # the demoted active section no longer leads with the old heading
    assert "### Active return vs passive" not in md
    # idle arm is 0% by construction note is present
    assert "idle arm" in md.lower()
```

Leave `test_render_markdown_handles_infinity_sortino_and_ir` unchanged — IR `Infinity`
still renders in the secondary diagnostic section, so its `assert "Infinity" in md` holds.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_oos_profitability.py::test_render_markdown_contains_key_sections -v`
Expected: FAIL on `assert "bot-vs-idle" in md` (string not yet in renderer output).

- [ ] **Step 3: Reframe the module docstring**

In `scripts/run_oos_profitability.py`, replace the first docstring paragraph (lines ~1–6).
Current:

```python
"""Canary OOS profitability characterization (backlog #4, reframed).

Runs the deployed canary config (MeanReversion x fUST x {a30,p2}) over rolling
1-month OOS windows vs the AlwaysMarketRate passive benchmark, and writes a research doc
with bootstrap CIs + a selection-bias deflated-Sharpe check.
```

New:

```python
"""Canary OOS profitability characterization (backlog #4, reframed).

Runs the deployed canary config (MeanReversion x fUST x {a30,p2}) over rolling
1-month OOS windows. Primary metric is the bot's absolute monthly return on budget
— i.e. bot-vs-idle, since an idle balance earns 0%. The AlwaysMarketRate comparison
(active return / information ratio) is a secondary, non-gating MR-timing-alpha
diagnostic. Writes a research doc with bootstrap CIs + a selection-bias
deflated-Sharpe check.
```

- [ ] **Step 4: Reframe the TL;DR block**

In `render_markdown`, replace the TL;DR block (currently lines ~107–118):

```python
    lines.append("## TL;DR\n")
    for r in reports:
        s, b = r.strat_summary, r.base_summary
        lines.append(
            f"- **{r.cell_label}** ({r.n_windows} months): strat median "
            f"{s.median_monthly:.4f}%/mo (annualized {s.annualized_pct:.2f}%), "
            f"baseline {b.median_monthly:.4f}%/mo; active median "
            f"{r.active.median_active:.4f}%/mo, IR {r.active.information_ratio}; "
            f"worst-month {s.worst_monthly:.4f}%, idle {s.idle_rate:.2%}; "
            f"deflated-Sharpe {r.deflated_sharpe:.4f}."
        )
    lines.append("")
```

with:

```python
    lines.append("## TL;DR\n")
    lines.append(
        "- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return "
        "on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle "
        "edge (idle arm = 0% by construction)."
    )
    for r in reports:
        s = r.strat_summary
        lines.append(
            f"- **{r.cell_label}** ({r.n_windows} months): bot-vs-idle median "
            f"{s.median_monthly:.4f}%/mo (annualized {s.annualized_pct:.2f}%); "
            f"worst-month {s.worst_monthly:.4f}%; idle {s.idle_rate:.2%}; "
            f"deflated-Sharpe {r.deflated_sharpe:.4f}."
        )
    lines.append(
        "- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** "
        "— near 0 means MR timing adds little over always-lending; it does NOT gate "
        "the bot-vs-idle headline. See per-cell sections."
    )
    lines.append("")
```

- [ ] **Step 5: Add the bot-vs-idle honesty-caveat bullet**

In `render_markdown`, the OOS honesty caveat block is currently a single
`lines.append("## OOS honesty caveat\n")` + one paragraph (lines ~120–127). After the
existing paragraph append, add one bullet mirroring the live report:

Find:

```python
    lines.append("## OOS honesty caveat\n")
    lines.append(
        "Deployed params were chosen by a sweep over this same 2022-2026 history, so "
        "these per-month returns are **in-sample to the parameter-selection process** — "
        "an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section "
        "quantifies the selection-bias haircut. The only true out-of-sample test is the "
        "live canary itself.\n"
    )
```

Append immediately after it:

```python
    lines.append(
        "- A positive bot-vs-idle backtest asserts the bot beats an idle balance "
        "(earns the market rate on budget), NOT that MR timing beats always-lending "
        "— that is the separate MR timing alpha diagnostic below.\n"
    )
```

- [ ] **Step 6: Relabel the per-cell table column and CI line**

In `render_markdown`'s per-cell loop, change the table header (line ~132) from:

```python
        lines.append("| Metric | Strategy | Baseline (AlwaysMarketRate) |")
```

to:

```python
        lines.append("| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |")
```

and change the bootstrap CI line (lines ~143–146) from:

```python
        lines.append(
            f"**Strategy median monthly 95% CI (bootstrap):** "
            f"[{r.median_ci[0]:.4f}%, {r.median_ci[1]:.4f}%]\n"
        )
```

to:

```python
        lines.append(
            f"**bot-vs-idle median monthly 95% CI (bootstrap):** "
            f"[{r.median_ci[0]:.4f}%, {r.median_ci[1]:.4f}%]\n"
        )
```

- [ ] **Step 7: Rename the active-return section to the secondary diagnostic**

In `render_markdown`'s per-cell loop, change the section heading + body (lines ~147–153) from:

```python
        lines.append("### Active return vs passive\n")
        lines.append(
            f"- median active: {r.active.median_active:.4f}%/mo; "
            f"mean active: {r.active.mean_active:.4f}%/mo\n"
            f"- information ratio: {r.active.information_ratio}\n"
            f"- months strategy > baseline: {r.active.pct_months_outperform:.2%}\n"
        )
```

to:

```python
        lines.append("### MR timing alpha vs passive (secondary diagnostic)\n")
        lines.append(
            "- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means "
            "MR timing adds little over always-lending; the product value is bot-vs-idle.\n"
            f"- median active: {r.active.median_active:.4f}%/mo; "
            f"mean active: {r.active.mean_active:.4f}%/mo\n"
            f"- information ratio: {r.active.information_ratio}\n"
            f"- months strategy > baseline: {r.active.pct_months_outperform:.2%}\n"
        )
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_oos_profitability.py -v`
Expected: PASS (both `test_render_markdown_contains_key_sections` and
`test_render_markdown_handles_infinity_sortino_and_ir`, plus `test_canary_yaml_loads_cells`).

- [ ] **Step 9: Run the full unit gate + ruff**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run ruff check`
Expected: all green, no ruff errors.

- [ ] **Step 10: Commit**

```bash
git add backend_py/scripts/run_oos_profitability.py backend_py/tests/scripts/test_run_oos_profitability.py
git commit -m "✨ Feat: OOS report leads bot-vs-idle headline + MR alpha secondary (G3 OOS reframe)"
```

---

### Task 2: Regenerate the committed research doc

**Files:**
- Modify: `docs/research/2026-05-28-canary-oos-profitability.md`
- Modify: `docs/research/2026-05-28-canary-oos-profitability.json`

This regenerates the offline report so the committed doc equals the new generator
output (and drops the stale `AlwaysFRR` label). Reads Neon historical candles only —
no canary/live exposure.

- [ ] **Step 1: Regenerate the report**

Run from `backend_py/` (module form is required — `python scripts/...` raises
`ModuleNotFoundError: scripts`):

```bash
cd backend_py && uv run python -m scripts.run_oos_profitability \
    --output ../docs/research/2026-05-28-canary-oos-profitability.md
```

Expected: writes the `.md` and sibling `.json`, logs `wrote ...`. If it exits with
`No candles for ...`, the candle table needs a backfill — STOP and report; do not
fabricate the doc.

- [ ] **Step 2: Verify the new framing landed in the doc**

Run: `grep -c "bot-vs-idle" ../docs/research/2026-05-28-canary-oos-profitability.md`
Expected: ≥ 3 (TL;DR definition + per-cell headline bullets + CI line). Also confirm
the stale label is gone:
Run: `grep -c "AlwaysFRR" ../docs/research/2026-05-28-canary-oos-profitability.md`
Expected: 0.

- [ ] **Step 3: Commit**

```bash
git add docs/research/2026-05-28-canary-oos-profitability.md docs/research/2026-05-28-canary-oos-profitability.json
git commit -m "📝 Docs: regenerate OOS report with bot-vs-idle framing (G3 OOS reframe)"
```

---

## Final verification

- [ ] `cd backend_py && uv run pytest -m "not integration" -q` — green
- [ ] `cd backend_py && uv run ruff check` — clean
- [ ] `git grep -n "Active return vs passive" backend_py/scripts/` — no matches (old heading gone)
- [ ] Regenerated doc contains `bot-vs-idle` and no `AlwaysFRR`
