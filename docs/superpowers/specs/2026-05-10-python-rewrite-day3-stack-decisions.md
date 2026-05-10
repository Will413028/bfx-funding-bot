# Decision: Python Rewrite — Day-3 Checkpoint Gating Stack Decisions

**Date**: 2026-05-10
**Status**: Confirmed via brainstorming session (Will + Claude, 2026-05-10 morning)
**Parent decision**: [`2026-05-09-backend-rewrite-to-python-decision.md`](./2026-05-09-backend-rewrite-to-python-decision.md) — see §Fresh-mind Review (2026-05-10) for the reframed 1-2 week timeline that constrains scope here
**Scope**: 3 of 15 sub-decisions from the parent doc. The remaining 12 are **deferred until Checkpoint 2 passes** (Week-1 end).

---

## Summary

| # | Decision | Choice |
|---|---|---|
| Q1 | Web framework | **FastAPI** (from day 1) |
| Q2 | DB access + migration tool | **SQLAlchemy 2.0 (async, typed) + Alembic** |
| Q3 | Bitfinex client | **Hand-roll httpx + websockets, no SDK dependency** |

Cross-decision consistency: Q1 (FastAPI) and Q2 (SQLAlchemy + Alembic) are **framework-heavy, battle-tested**. Q3 (hand-roll Bitfinex client) is **dependency-light, full-control**. The asymmetry is intentional: framework adoption is high-value where the ecosystem has solved hard problems (HTTP routing, async ORM, migrations); dependency rejection is high-value where the SDK adds no abstraction over a `httpx.get()` call yet brings supply-chain + version-bump + test-coverage risk. Both stances apply the same underlying principle — adopt or reject based on **actual value delivered per unit of risk**, not framework-heaviness as an end in itself. The trade-off accepted is that "1-week port" is on the optimistic end of the P50–P90 band; the abort criteria in the parent doc remain in force unchanged. **User confirmed acceptance of this framing 2026-05-10.**

---

## Q1 — Web framework: FastAPI

### Choice
FastAPI from the first commit of the rewrite.

### Rationale
- **Pydantic-native**: Q3 produces Pydantic domain models; FastAPI consumes them as request/response schemas with no glue code. Single type system end-to-end.
- **OpenAPI auto-gen**: Frontend (Next.js 16) can codegen types directly from the FastAPI schema, removing one class of contract-drift bugs that the Go side never solved automatically.
- **Avoids a second framework switch later**: If we picked "no-web now, framework later," we would brainstorm framework choice again under time pressure during Stage 2. Doing it now amortizes that cost.
- **Ecosystem momentum**: largest async-Python web framework community; debugging path is well-trodden.

### Trade-offs accepted
- Day-3 / Week-1 checkpoint pass criteria are **unchanged** by this choice — adding FastAPI does not give us license to substitute "the API serves a request" for "fetch → write → read back matches Bitfinex source numbers." The web layer is non-blocking for checkpoint validation.
- ~Half a day of FastAPI scaffolding (router, app factory, settings wiring) is taken out of the 1-week budget. Acknowledged.
- Litestar's per-request perf edge and msgspec integration are forgone. For Bitfinex's 30 req/min rate ceiling, this is irrelevant.

### Rejected alternatives
- **No-web (pure CLI / scripts)** — Was Claude's recommended option on grounds of "checkpoint doesn't need it." User overrode: doing the framework choice now is worth the slight timeline cost vs. revisiting under pressure.
- **Litestar** — Smaller ecosystem; faster but the perf gap doesn't matter for this workload; would also force a SQLModel-vs-SQLAlchemy bias-shift in Q2.

---

## Q2 — DB access: SQLAlchemy 2.0 + Alembic

### Choice
SQLAlchemy 2.0 with async session, typed `Mapped[]` annotations, and the 2.0 `select()` API. Migrations via Alembic.

### Rationale
- **Industry standard**: largest community, deepest documentation, most stable long-term maintenance trajectory among Python ORMs.
- **Type-safe in 2.0**: `Mapped[]` annotations + mypy strict gives compile-time-ish safety for table models — the closest Python gets to the sqlc/pgx-typed experience the Go side has today.
- **Handles complex queries cleanly**: backtest workloads include time-series aggregations (`GROUP BY hour`, window frames, percentile aggregates). SQLAlchemy 2.0's `select()` API expresses these without dropping to raw strings, and supports raw SQL escape hatches when needed.
- **Async-first**: aligns with FastAPI's async runtime.

### Migration tool: Alembic (not Atlas)
Original parent doc Q5 (keep Atlas vs. switch to Alembic) is **decided here**: switch to Alembic.

- Tighter integration with SQLAlchemy 2.0 model autogeneration (`alembic revision --autogenerate` reads the declarative models directly)
- One less tool in the loop (Atlas is HCL-DSL + Go-tool; Alembic is pure-Python, in-repo)
- Loses Atlas's lang-agnostic portability — accepted, since the rewrite ends the dual-language situation that made portability valuable

### Atlas → Alembic transition: baseline-from-DB
Decided 2026-05-10 (this session): **option (a) baseline-from-DB**.

Procedure on Day 1 of rewrite:
1. Define SQLAlchemy 2.0 declarative models matching the current Neon schema (users, api_keys, user_configs, executions, funding_candles, funding_stats, ...)
2. Run `alembic revision --autogenerate -m "baseline from existing schema"` against the current Neon database
3. Verify the autogenerated migration is **empty** (no `op.create_table`, no `op.add_column`) — if non-empty, the SQLAlchemy models drift from actual schema; fix models before stamping
4. `alembic stamp head` to mark the current DB state as the baseline revision without re-running anything
5. Archive `backend/schema/migrations/*.sql` Atlas migration history under `backend/schema/migrations.atlas-archived/` as historical record only; remove Atlas from active tooling

Rationale for (a) over (b):
- Forces SQLAlchemy models to be exhaustively defined Day 1 (catches model/schema drift early — better than discovering it weeks later when adding a feature)
- Single revision history going forward; no special "revision 0 is empty" footnote in onboarding
- The autogenerate-empty check is itself a useful Day-1 sanity gate

Risk if autogenerate is **not** empty (model drift detected): fix the SQLAlchemy models first, re-run autogenerate until empty, **do not** apply the non-empty diff (the schema is the source of truth, not the freshly-written models). If the drift is irreconcilable within ~half a day, fall back to option (b) without ceremony.

### Trade-offs accepted
- **vs. psycopg3 raw + Pydantic typed wrapper**: gives up the "thinnest possible" path. Accepted because backtest's complex aggregations would push us back into ORM-land later anyway, and the unit-of-work / session lifecycle cost is paid once in setup, not per-query.
- **vs. SQLModel**: gives up "model-defined-once" between ORM and API schema. Accepted because SQLModel's abstraction leaks to SQLAlchemy under any non-trivial query, the maintenance cadence is slower, and 2024–2026 sharp edges are still present. We will hand-write the `Domain ↔ ORM ↔ API` mapping where it differs (often it won't, since Pydantic models are usable as both ORM-result containers and API DTOs via `model_validate`).

### Rejected alternatives
- **psycopg3 raw + own typed wrapper** — Was Claude's recommended option (closest to current sqlc mindset, lowest abstraction tax for a 1-week port). User overrode: prefer industry-standard ORM for long-term maintenance over short-term portability.
- **SQLModel** — Inconsistent with "framework-heavy but battle-tested" theme of the other two decisions; FastAPI-author-tied risks single-maintainer cadence problems.

---

## Q3 — Bitfinex client: Hand-roll httpx + websockets, no SDK

### Choice
Hand-roll the Bitfinex client directly using `httpx` (REST) and `websockets` (or `aiohttp`) (WS). No `bitfinex-api-py` dependency. Pydantic domain models are the only types upstream code sees.

Layer structure:

```
infra/bitfinex/
  rest.py            # async httpx client: get_funding_candles, ticker, book, ...
  ws.py              # async websockets client: public + auth funding channels
  auth.py            # HMAC-SHA384 request signing for private endpoints
  rate_limit.py      # aiolimiter.AsyncLimiter — 30 req/min per funding endpoint group
  errors.py          # typed exceptions: RateLimited, AuthFailed, BitfinexAPIError
domain/
  models.py          # FundingCandle, FundingOrder, FundingOffer, FundingLoan, FundingCredit (Pydantic)
```

Upstream code (strategy / backtest / repository / API handlers) imports only from `domain/models.py`.

### SDK verification + reframing (2026-05-10, this session)

Verified `bitfinexcom/bitfinex-api-py` v4.0.0 against actual GitHub source before deciding:

- ✅ All funding endpoints wrapped (candles, ticker w/ FRR, book, stats, offer submit/cancel, loans/credits, history, WS public + auth)
- ✅ Full type hints + dataclasses, recent maintenance (v4.0.0 = 2025-10-18)
- ⚠️ REST client is synchronous (uses `requests`, not `httpx`) — async-bridging needed if used with FastAPI
- ⚠️ No built-in rate limiting — must add anyway
- ⚠️ **No `tests/` directory in the repo** — production-quality unverifiable, user becomes the QA
- ⚠️ v4.0.0 is a recent rewrite (PR #254) — new bugs not yet surfaced by community use

### Why hand-roll won — historical context provided by user

The Go backend (`internal/bitfinex/`, ~14k REST + ~23k WS) was hand-rolled. **Reason was not "Go SDK was bad" — the Go SDK was so thin that hand-rolling was equivalent cost with full control.** Same logic applies to `bitfinex-api-py`:

- Total SDK source is ~4250 lines, dominated by dataclass definitions
- The actual REST plumbing is a thin wrapper around `requests.get(url, params=...)` returning typed dataclasses
- Hand-rolling that with `httpx` + Pydantic models gives equivalent function with: full async-native, full rate-limit control, full retry/backoff control, zero dependency surface, zero "v4.0 just-shipped" risk, zero "no tests in upstream" risk

For Day-3 scope (one endpoint: `/v2/candles/trade:1h:fUST/hist`):
- Hand-roll cost: ~50–60 lines (httpx call + 1 Pydantic model + 1 mapper). Half a day.
- SDK route cost: install + read SDK docs + figure out dataclass shape + bridge sync→async + add rate limiter externally + carry SDK risk. Also half a day, but with risk attached.

The work doesn't get cheaper by adding a dependency that wraps `requests.get`.

### Rationale
- **Symmetry with Go choice + same reasoning**: the original Go decision logic applies unchanged in Python. SDK is too thin to add value.
- **Async-native from line one**: no `asyncio.to_thread` bridging, integrates cleanly with FastAPI async runtime + async SQLAlchemy.
- **Full control over real-money path**: rate limiting, retry/backoff, error mapping, idempotency keys for offer submission — all owned in our codebase, all testable, all visible in stack traces. Critical for a real-money bot.
- **Zero supply-chain / version-bump exposure**: no SDK updates to track, no breaking API changes from upstream, no surprise behavior shifts.
- **Test discipline maintained**: we own the tests; this is required for the lending engine anyway (it's the IP).
- **Pydantic models are the same regardless**: SDK route or hand-roll route both need `domain/models.py`. Hand-roll route just removes one indirection (no SDK dataclass → mapper → domain model; just httpx response → Pydantic `model_validate`).

### Trade-offs accepted
- We carry the burden of tracking Bitfinex REST API changes ourselves (paginate semantics, new fields, deprecations). Mitigation: only wrap endpoints we use; small surface area; same burden the Go side already carries with no major incident in production.
- WebSocket auth + reconnect + heartbeat handling is non-trivial (Go side is ~900 lines for this). Day-3 doesn't need WS — defer to whenever WS becomes critical-path (Stage 2+).
- We forgo any future improvement to `bitfinex-api-py` (e.g., new endpoint wraps). Accepted: the forgone improvement is "they wrote our `httpx` call for us," which is not a meaningful loss.

### Day-3 scope for hand-rolled client

Only the **single REST endpoint** needed for Checkpoint 1:
- `GET /v2/candles/trade:{tf}:f{symbol}/hist` with `start`, `end`, `limit` parameters
- Response array → `FundingCandle` Pydantic model
- httpx async client + aiolimiter for the 30 req/min rate cap
- No auth (public endpoint), no WS, no error retry beyond simple 429 backoff

Estimated: 50–60 lines of `infra/bitfinex/rest.py` + ~15 lines of `domain/models.py` + ~30 lines of pytest. ~Half a day.

### Rejected alternatives
- **`bitfinex-api-py` direct use** — Adds a thin wrapper around `requests.get` that we have to bridge to async; brings risk (no tests in upstream, recent v4.0 rewrite); user has prior experience that the SDK doesn't add value over hand-rolling.
- **`bitfinex-api-py` + thin wrapper** (the Q3 choice from the first round of this session) — Earlier reasoning was "SDK removes burden, wrapper insulates." After verifying SDK quality concerns and user's prior context (Go was hand-rolled for the same reason: SDK adds no value over thin httpx call), this no longer holds.
- **`ccxt` library** — Generic multi-exchange abstraction with funding endpoints exposed only as raw method calls (no unified abstraction); larger dependency, less typed, no funding-specific value.

### Parent doc reframing required

The parent doc's 5/10 fresh-mind review listed as the 4th surviving reason for the rewrite:

> "`bitfinex-api-py` first-class funding support — removes the 'hand-rolled API client' maintenance burden the Go side carried."

This rationale is **partially retracted in light of this Q3 decision**. The accurate version:

> "Python ecosystem (`httpx` + `websockets` + Pydantic + native async) makes hand-rolling the Bitfinex client **substantially cheaper than the Go equivalent** — Day-3 needs ~50 LoC instead of Go's ~14k REST client. The 'hand-rolled API client' is not a burden in Python, regardless of whether `bitfinex-api-py` exists. SDK availability does not factor into the rewrite rationale."

Net effect on parent doc: rewrite still has 4 surviving reasons, but reason #4 is now "Python's hand-roll is cheap" rather than "SDK exists so we don't need to hand-roll." Update to be made in a follow-up edit to `2026-05-09-backend-rewrite-to-python-decision.md`.

---

## Cross-decision consistency check

The three choices form a coherent stack with shared assumptions:

- **Type safety first**: Pydantic + SQLAlchemy 2.0 typed + own domain models — every layer boundary is type-checked.
- **Framework-heavy, battle-tested**: FastAPI + SQLAlchemy + Alembic are all top-of-ecosystem choices. Bet is on long-term maintainability, not short-term LoC.
- **Single type system through-line**: `domain/models.py` Pydantic models flow from Bitfinex wrapper → strategy → repository → API response with minimal translation.
- **Internal boundary protects the IP**: strategy / backtest code (the actual product value) never sees SDK types, never sees ORM internals, never depends on web-framework concerns.

The trade-off accepted in aggregate: this is **not** a "minimum viable port." It is a "minimum viable port with the boundaries that future-Will will not have to refactor." If the 1-week P50 estimate slips toward 1.5 weeks because of this, that is acknowledged and within the P90 budget. **The Week-2 hard abort line in the parent doc is unchanged.**

---

## Day-3 checkpoint — stack-specific pass criteria (refines parent doc)

Parent doc specifies: *"`bitfinex-api-py` pulls fUST 1h candles → writes into existing `funding_candles` PostgreSQL table → reads back via DB query → matches Bitfinex source numbers."*

Stack-specific concretization for these decisions:

1. ✅ FastAPI app boots (`uvicorn app.main:app` returns 200 on `/health`)
2. ✅ SQLAlchemy 2.0 async engine connects to Neon, can `SELECT 1`
3. ✅ Alembic baseline strategy chosen (a or b above) and applied; `alembic current` returns a revision
4. ✅ `infra/bitfinex/rest.py` hand-rolled httpx client has `get_funding_candles(symbol, timeframe, start, end, limit=125)` returning `list[FundingCandle]`, with aiolimiter rate limiting active
5. ✅ End-to-end: a CLI script (`scripts/backfill_candles.py`) calls REST client → parses to `FundingCandle` → writes via SQLAlchemy → a separate query reads back → numbers match a manual Bitfinex web-UI sample exactly (no rounding tolerance — any drift indicates a Decimal/float bug worth catching now)

Failure of any of #4 or #5 = abort per parent doc Checkpoint 1 rules. Failures of #1–#3 are stack-setup friction; if they take more than ~1 day cumulatively, that is itself a signal worth flagging at Day-3 review even if technically "passing."

## Day-3 checkpoint — stack-specific abort signals

Beyond the parent doc's generic abort triggers, these stack-specific signals warrant escalation:

- **SQLAlchemy 2.0 async + asyncpg / psycopg3 driver friction** that consumes more than half a day → red flag (driver pain compounds; this should be a solved problem)
- **Alembic autogenerate produces incorrect or unusable baseline** → switch to forward-only baseline (option b) immediately, do not debug autogenerate
- **Hand-rolled Bitfinex REST client surprises** — Day-3 needs only one endpoint (`get_funding_candles`), so this should be small, but watch for:
  - Bitfinex's funding candle endpoint returning unexpected field count or ordering vs. their public docs (their API has historical inconsistencies between asset classes)
  - Pagination semantics: `start`/`end` are **millisecond** timestamps (not seconds); `limit` defaults to 125 with max 10000 — easy to get wrong
  - Rate limit response (429) handling: aiolimiter prevents most 429s; if we still hit them, the limit is per-IP across endpoint groups, not per-endpoint
  - Numeric precision: Bitfinex returns rates as floats; ensure Pydantic uses `Decimal` for any field that flows into financial math (parent doc parity check requires exact match to source)

  **Threshold**: if hand-roll for the single Day-3 endpoint takes more than half a day (one work session), pause and ask why — the design point is "this is comparable cost to using the SDK"; significant blowout suggests we missed something fundamental about httpx async patterns or Bitfinex's API quirks, and the broader Python rewrite assumption ("Python ecosystem makes hand-rolling cheap") is at risk.

---

## Frontend during rewrite window: also frozen

Decided 2026-05-10 (this session): Next.js frontend is **frozen alongside the Go backend** for the duration of the rewrite. No frontend feature work, no API contract changes against the Go backend.

Rationale:
- Stage 1.5 backtest go/no-go has not passed → no live users → frontend is for solo internal use only → freezing it costs nothing
- Frontend resumes after Python backend is functional enough to point at; first frontend change post-rewrite will be repointing API base URL + regenerating types from FastAPI's OpenAPI schema
- Removes one source of cross-cutting churn during the 1-2 week window

Not deferred, decided.

## Open questions deliberately deferred
- Concurrency model for many users (asyncio task per user vs. Celery vs. Ray actors) — parent doc Q9, deferred until after Checkpoint 2
- Real-money safety / feature-flag library — parent doc Q11, deferred until after Checkpoint 2
- Observability stack (structlog + OpenTelemetry vs. keep Axiom) — parent doc Q13, deferred until after Checkpoint 2
- Frontend contract evolution (REST + codegen vs. GraphQL via strawberry) — parent doc Q15, post-Stage-1.5

---

## What this decision does NOT change

- Parent doc's abort criteria and Week-2 hard line — **in force unchanged**
- Parent doc's revised "Why" (developer fit + Polars upgrade + ops simplicity + bitfinex-api-py parity) — **the actual justification for the rewrite at all**
- Strategy / backtest engine design — **next brainstorming session**, not this one
- Existing Atlas migrations already applied to Neon — schema stays as-is, only the migration tool that moves it forward changes
