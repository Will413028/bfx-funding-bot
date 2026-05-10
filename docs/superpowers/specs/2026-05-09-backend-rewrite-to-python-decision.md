# Decision: Rewrite Backend from Go to Python

**Date**: 2026-05-09 (initial), **2026-05-10 fresh-mind reviewed**
**Status**: Direction confirmed; framing revised. See [Fresh-mind Review (2026-05-10)](#fresh-mind-review-2026-05-10) at bottom — **read it before acting on anything in the original 5/9 body**, several rationales were retracted under questioning.
**Source**: superpowers brainstorming session 2026-05-09 evening (Will + Claude); fresh-mind 5-question challenge 2026-05-10 morning (Will + Claude)

---

## Decision

Rewrite the entire backend from Go (~14k lines) to Python in a **big-bang migration**:
- Freeze current Go backend (no further feature work)
- Rewrite backend in Python (~6-12 weeks estimated)
- Resume Stage 1.2-1.5 validation work on Python backend
- Frontend (Next.js 16) unchanged

## Why

### Goal restatement (user-confirmed)

Optimize for two things, ignoring rewrite cost:
1. **Strategy discovery efficiency** — find effective strategies fast
2. **Production performance** — run those strategies reliably for users

### Honest analysis

**Goal 1 (research velocity)**: Python wins clearly.
- Polars (Rust-backed DataFrames) for vectorized backtest
- pandas / numpy / statsmodels / scikit-learn / PyMC
- Jupyter / Marimo notebooks for second-cycle iteration
- optuna / Ray Tune for hyperparameter sweeps
- Plotting (matplotlib / plotly / seaborn) for visual feedback
- **Go has zero competitor in any of these.** Research speed differential 5-20x.

**Goal 2 (production performance)**: For a Bitfinex funding bot, "performance" ≠ raw lang perf.
- Bitfinex rate-limits funding endpoints to 30 req/min → not latency-sensitive
- Throughput at 1k users × 1 decision/min = trivial
- Real bottlenecks are: reliability, type safety in real-money path, ops simplicity
- Python with Pydantic strict + mypy strict gets to ~80% of Go's compile-time guarantees
- Solo dev ops simplicity: single language stack >> dual stack

**Initial recommendation was wrong.** Earlier in the session, I recommended "split brain" (Python research + Go production) on grounds that 14k Go is sunk-cost-precious. User correctly reframed: "ignore rewrite cost, what's optimal?" Under that framing, **Single Python wins** because:

- Strategy code written ONCE (no Python→Go port gap; same `Strategy` class for backtest + paper + live)
- Polars closes the historical "Python is slow" perf gap for vectorized ops
- Single ops surface for solo dev with multi-job context
- Iteration velocity: Python edits → live without redeploy via strategy-as-config

**Critical research finding (subagent survey 2026-05-09)** that further weakened the "keep Go" argument:
- freqtrade / hummingbot don't support funding/lending markets — fork-and-extend impossible (architectural mismatch: rate × period_days vs price × qty)
- ccxt has Bitfinex funding endpoints as raw methods (no unified abstraction)
- bitfinex-api-py (official Bitfinex Python SDK) has full first-class funding REST + WebSocket support — **direct replacement for the self-built Go client**
- No Python backtest framework supports lending mechanics — **backtest engine has to be hand-written either way** (no Go vs Python advantage on this)

The combination: Python's research advantage is real, Python's API client parity with Go is real, Python's production path is acceptable for this product's scale and latency profile.

## What this invalidates / preserves

| Artifact | Status | Action |
|---|---|---|
| `wiki/tech/bitfinex-funding-rate-data-sources.md` | ✅ Valid (lang-agnostic) | Keep |
| Atlas migration `20260509100512_add_funding_history_tables.sql` (already applied to Neon) | ✅ Valid (DB-agnostic) | Keep schema; Python backend reads/writes same tables |
| OpenSpec change `bitfinex-funding-historical-backfill` | ⚠️ Partially obsolete | Mark abandoned tomorrow; design.md decisions D1-D8 portable; D9-D10 (testing convention / cursor pure func) are Go-specific |
| `docs/superpowers/plans/2026-05-09-bitfinex-funding-historical-backfill.md` (Go impl plan, ~80 TDD steps) | ❌ Obsolete | Keep file as historical record (the path not taken); do not execute |
| Existing 14k Go (lending engine + auth + bitfinex client + handlers + middleware + service + repository) | ❌ To be replaced | Freeze main; rewrite from new branch / new repo TBD |
| `wiki/projects/bfx-funding-bot.md` Stage 1.2 sub-tasks | ⚠️ Tasks valid in spirit, Go specifics out | Update to reflect Python target after rewrite design session |
| Frontend Next.js 16 | ✅ Unchanged | API contract changes (FastAPI auto-OpenAPI → frontend codegen) |

## Path: Big-bang (chosen over Strangler Fig and Hybrid)

User explicitly chose big-bang over:
- **Strangler Fig** (split-then-merge: A then port if validates) — rejected because would do work twice
- **Hybrid** (Python rewrite but minimal viable, defer full lending engine) — rejected for ambiguity in "minimal" boundary

Big-bang trade-offs accepted:
- ✅ Single architecture commitment (no two-stack maintenance during transition)
- ✅ Strategy code path unified end-to-end on day one
- ⚠️ Validation timeline pushed back 6-12 weeks
- ⚠️ All-eggs-one-basket: if Python rewrite hits unexpected issues, no fallback to "just iterate the Go path"
- ⚠️ During rewrite window, no live progress on Stage 1.5 backtest go/no-go

## Pending sub-decisions (next brainstorm session)

15 sub-decisions to make in next session, organized by layer:

### Stack
1. **Web framework**: FastAPI vs Litestar vs Robyn
2. **Type safety stack**: Pydantic v2 strict + mypy strict + ruff configs
3. **Concurrency model**: pure asyncio per-user task vs arq/Celery batch vs actor lib (dramatiq)
4. **DB access**: sqlmodel (high-level) vs sqlalchemy (low-level) vs psycopg3 raw + own typed wrappers
5. **Migration tool**: keep Atlas (lang-agnostic, already in use) vs switch to alembic
6. **Bitfinex SDK**: bitfinex-api-py (official) vs ccxt vs hand-roll like Go version
7. **Strategy framework**: BaseStrategy ABC + plugin loader pattern
8. **Backtest engine**: Polars-based custom (recommended) vs vectorbt-inspired

### Production concerns
9. **Worker model for many users**: asyncio task per user vs Celery vs Ray actors
10. **Auth/crypto**: keep JWT same format (RS256)? AES-256-GCM via cryptography lib?
11. **Real-money safety**: feature flag library (e.g., flagr / unleash-client) + hot reload + kill switch design
12. **Testing strategy**: pytest + hypothesis + testcontainers for postgres
13. **Observability**: structlog + OpenTelemetry (replace Axiom integration?)
14. **Config**: pydantic-settings
15. **Frontend contract**: keep REST? Add openapi-codegen for frontend types? Try GraphQL via strawberry?

## Next action items (tomorrow / next session)

When fresh, run `/superpowers:brainstorming` with topic:
> "Detail the Python backend architecture for the bfx-funding-bot rewrite. Decision doc: `docs/superpowers/specs/2026-05-09-backend-rewrite-to-python-decision.md`. Walk through 15 sub-decisions in order, lock high-level architecture before drilling into each."

Pre-session prep checklist:
- [ ] Review this decision doc with fresh mind — confirm "Yes, still want big-bang Python"
- [ ] Mark OpenSpec change `bitfinex-funding-historical-backfill` abandoned (run `openspec archive` with abandoned status, or move to `openspec/changes/abandoned/`)
- [ ] Update `wiki/projects/bfx-funding-bot.md` Stage 1.2 sub-tasks to "blocked: Python rewrite in progress"
- [ ] Decide: rewrite in-place (`backend/` directory) vs new repo `bfx-funding-bot-py/`. Affects git history + deployment cutover strategy.
- [ ] Decide: who is the rewrite ICP — solo Will only, or a hire? Affects framework / lib popularity considerations.

## Open questions deliberately NOT answered tonight

- Exact 6 vs 12 week timeline (depends on stack decisions)
- Cutover strategy (parallel run? hard switch?)
- Whether existing OpenSpec workflow continues for rewrite or is replaced (CLAUDE.md just changed to Superpowers — confirm no contradiction)
- Whether to take this opportunity to also re-think frontend (still Next.js 16, but maybe re-think tRPC vs REST during this window)

These are NOT blockers for tomorrow's brainstorm but should be flagged.

---

## Decision authority

Sole decision-maker: Will (solo founder).
Brainstormed in superpowers session with Claude on 2026-05-09 (Saturday evening).
No external review required.

> ⚠️ The original 5/9 reversibility paragraph («2-3 weeks proving period, then commitment hardens») is **retracted** as a sunk-cost / escalation-of-commitment trap with no defined abort criteria. See [Fresh-mind Review (2026-05-10)](#fresh-mind-review-2026-05-10) for the corrected abort criteria.

---

## Fresh-mind Review (2026-05-10)

Saturday-night decisions need Sunday-morning review. Claude challenged the decision with 5 honest questions; below is what survived.

### What got retracted

| Original 5/9 claim | 5/10 status | Why |
|---|---|---|
| "Research velocity differential 5-20x (Polars vs Go)" | ❌ **Retracted as vibe estimate** | Funding rate strategy math is `rate × period × notional - fee`, not quant ML. No specific iteration step was identified that Python enables and Go blocks for *this product's* use case. |
| "Strategy code written ONCE avoids Python→Go port gap" | ❌ **Retracted as imagined pain** | 0 strategies written today; the "port gap" is hypothetical, not observed. Alternative (Python notebook research → one-time Go port of validated strategies) was not analyzed against actual iteration count expectation. |
| "Big-bang rewrite, 6-12 weeks estimated" | ⚠️ **Reframed as quick re-do, P50 1 week / P90 2 weeks** | Self-estimate on fresh-mind review is 6-12× shorter than original doc. Implies either (a) bulk of 14k Go is generated/boilerplate, (b) port is not 1:1 — only core logic re-implemented, peripherals re-designed, or (c) heavy LLM-assisted translation. Either way, the dramatic "big-bang + commitment hardens + Stage 1.5 frozen for 6-12 weeks" framing is not the real shape of the work. |
| "All-eggs-one-basket, no fallback" | ⚠️ **Risk overstated under revised timeline** | At P50 = 1 week, the "no fallback" risk window is small enough that it is not a primary concern; properly bounded by abort criteria below. |

### What survived

| Original 5/9 claim | 5/10 status |
|---|---|
| Direction: Single-stack Python | ✅ **Confirmed** — gut still says Python after challenge |
| `bitfinex-api-py` parity with self-built Go client | ⚠️ **Reframed 2026-05-10 in Q3 brainstorm** — SDK was empirically verified usable (v4.0.0, all funding endpoints wrapped), but Q3 decision was to **hand-roll httpx + websockets instead**. SDK is too thin to add value over a direct `httpx.get()` call, brings supply-chain / no-tests-in-repo / v4.0-recency risk, and Go was hand-rolled originally for the same reason (SDK adds no abstraction over the underlying HTTP call). See `2026-05-10-python-rewrite-day3-stack-decisions.md` §Q3 for full reasoning. |
| Polars / Jupyter is a real research-experience upgrade for *this developer* | ✅ Confirmed (developer-fit reason, not abstract velocity reason) |
| Solo-dev ops simplicity from single-stack | ✅ Confirmed |
| `wiki/tech/bitfinex-funding-rate-data-sources.md` + Atlas migrations stay valid | ✅ Confirmed |
| Frontend Next.js 16 unchanged | ✅ Confirmed |

### Revised "Why" (the real reasons, replacing the retracted ones)

The honest version of why Single Python wins for *this developer on this product*:

1. **Developer stack fit** — Will's Python familiarity ≥ Go familiarity for research/data-frame work; subjective experience matters for solo founder iteration energy.
2. **Tooling experience upgrade** — Polars + Jupyter/Marimo is a real ergonomic improvement *as a workspace*, even if the raw speed differential is unquantified for this product's scale.
3. **Single ops surface** — solo dev with multi-job context (apmic / dailyfresh / freelance) values one stack over two.
4. **Python ecosystem makes hand-rolling the Bitfinex client cheap** — `httpx` + `websockets` + Pydantic + native async = ~50 LoC for the Day-3 critical endpoint, vs. Go's ~14k REST + 23k WS. The "hand-rolled API client" is not a burden in Python regardless of whether `bitfinex-api-py` exists. **Reframed 2026-05-10 in Q3 brainstorm** — the original phrasing ("`bitfinex-api-py` removes the hand-rolled burden") wrongly implied SDK availability was the value driver; the actual value driver is Python ecosystem maturity, which holds whether we use the SDK or not.

These are honest developer-fit reasons. They are weaker than the retracted "5-20x velocity / strategy-port-gap" claims but they are *true*, which makes the decision durable.

### Revised timeline

| Estimate | Original (5/9) | Fresh-mind (5/10) |
|---|---|---|
| P50 | ~6 weeks | **1 week** |
| P90 | ~12 weeks | **2 weeks** |

The original timeline was anchored to "big-bang rewrite of 14k LoC". The actual work is closer to "quick port of core lending engine + re-design of peripherals leveraging Python ecosystem", which is much smaller. Concrete validation of timeline is the **Day-3 checkpoint** below.

### Abort criteria (replaces "commitment hardens after 2-3 weeks")

Hard, dated checkpoints. If a checkpoint fails, abort means **return to Go branch and resume Stage 1.2** — not "give it one more week."

#### Checkpoint 1: Day 3 (end-of-day Wednesday after rewrite start)

**Pass criterion**: end-to-end minimal path works — `bitfinex-api-py` pulls fUST 1h candles → writes into existing `funding_candles` PostgreSQL table → reads back via DB query → matches Bitfinex source numbers.

- ✅ Pass → continue to Checkpoint 2
- ❌ Fail (SDK incompatibility / DB driver pain / async pattern unclear) → **abort**: revert to Go, document blocker, resume Stage 1.2 in Go
- 🚧 Partial (works but ugly) → continue **with explicit note** in next session of what felt wrong

#### Checkpoint 2: Week 1 end (Friday end-of-day)

**Pass criterion**: backtest engine skeleton runs + 1 baseline strategy executes against historical fUST data and produces a monthly return + drawdown number.

- ✅ Pass → commitment confirmed; enter Stage 1.4-1.5 strategy iteration phase
- ❌ Fail → **abort**: P50 estimate was wrong, 14k Go is not 1-week reproducible in Python, return to Go
- ⚠️ Partial (engine runs but hand-rolled around library limits) → continue but document specific friction; re-evaluate at Week 2

#### Checkpoint 3: Week 2 end (P90 ceiling)

**Hard abort line.** If baseline strategy + backtest is not running by Week 2 end, **return to Go, no extension permitted.**

Rationale: the only justification for Python rewrite was developer-fit + Polars upgrade + ops simplicity. None of those reasons survive a 3+ week timeline blowout, because at that point the *real* cost is Stage 1.5 backtest go/no-go being frozen, which is the entire startup-validation purpose of this project.

### What the fresh-mind review changes for the next session (Phase 3)

When running detailed-architecture brainstorm:

- **Drop the "big-bang" framing** in stack discussions. The work is "quick port"; sub-decisions should be optimized for 1-2 week execution, not 6-12 week elaborate design.
- **De-prioritize sub-decisions that don't matter at 1-week scale**: e.g., elaborate observability stack, frontend GraphQL re-think, advanced worker model — these are over-engineering for a quick port.
- **Prioritize sub-decisions that gate Day-3 checkpoint**: web framework (or none — pure CLI?) / DB access / Bitfinex SDK usage. Those 3 must be settled to start work.
- **Defer post-Week-1 sub-decisions** (concurrency model for many users, real-money safety flags, observability) until after Checkpoint 2 passes — they are wasted analysis if abort triggers.
