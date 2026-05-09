# Decision: Rewrite Backend from Go to Python (Big-bang)

**Date**: 2026-05-09
**Status**: Decision locked. Detailed architecture brainstorm deferred to next session.
**Source**: superpowers brainstorming session 2026-05-09 evening (Will + Claude)

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
Reversible? Theoretically yes — can revert to Go branch if Python rewrite proves untenable. But practically the cost-of-reversal grows fast with sunk Python time. **First 2-3 weeks of Python work is the "is this actually working" proving period; beyond that, commitment hardens.**
