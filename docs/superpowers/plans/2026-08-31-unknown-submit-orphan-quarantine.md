# UNKNOWN Submit Outcome and Orphan Quarantine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 對任何可能已到達 Bitfinex 但本地失去 response 的 submit 建立 typed UNKNOWN、pessimistic exposure 與 symbol-level block；對 venue 上找不到本地 provenance 的 active offer 建立可稽核 quarantine，不再 retry 或製造 synthetic identity。

**Architecture:** submit pipeline 使用 SubmitAcknowledged、SubmitRejected、SubmitOutcomeUnknown、SubmitNotSent 四種結果；每個 execution decision 最多一個 submission_attempt。AccountCommandGate 將 guards → intent → network → outcome 串成有明確 durable boundary 的 command。UNKNOWN 與 orphan 都寫 immutable event + execution_uncertainties projection，先把 intended amount 計入 uncertain exposure 並只阻擋相關 account/environment/symbol。Fresh full-account reconcile 以 exact candidate/history match adopt；零候選保持 UNKNOWN，多候選需人工處理。

**Tech Stack:** Python 3.13、asyncio、httpx、SQLAlchemy 2.0 async、Alembic、FastAPI、Pydantic、pytest、PostgreSQL、Next.js 16、React Query。

**Spec:** docs/superpowers/specs/2026-08-31-production-integrity-foundation-design.md（UNKNOWN/orphan handling）。

## Global Constraints

- timeout、connection reset、5xx after request start、malformed response、cancel after start、process restart with unresolved intent 一律 UNKNOWN；不得由 exception type 猜測為 rejected，也不得自動 retry。
- local validation failure before transport is SubmitNotSent；explicit 2xx with venue ID is acknowledged；allowlisted structured 4xx is rejected；其他 result default UNKNOWN。
- One execution decision has one submission_attempt and one client correlation id. Exact normalized payload and SHA-256 fingerprint are immutable audit values; secrets/headers never persist。
- UNKNOWN 必須先 durable block/uncertainty 再允許下一個 command check；DB/read failure 在 command gate 內 fail closed。
- Uncertainty amount is added to gross exposure pessimistically and is not removed by retry. Only a fresh reconcile evidence event may resolve it.
- Orphan venue objects are persisted and counted; they receive no synthetic CID, strategy, reference, or automatic cancel. Quarantine scope is account/environment/symbol and does not stop other symbols。
- Matching requires all identity fields (venue ID, symbol, amount_original, rate, period, mts_created or exact history equivalent) and complete history coverage. Absence is not proof of rejection; multiple candidates stay UNKNOWN。
- Operator resolution API requires fresh reconcile fence and immutable operator ID; every resolution is an event, never an UPDATE-only flag。
- All tests use fake venue transport; no test may call Bitfinex. Migration uses Alembic from backend_py/。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| backend_py/src/bfx_funding_bot/modules/execution/submit_outcomes.py | typed submit outcome union and classification helpers |
| backend_py/src/bfx_funding_bot/modules/execution/command_gate.py | guard/intent/network/outcome sequencing and fail-closed uncertainty gate |
| backend_py/src/bfx_funding_bot/modules/execution/uncertainties.py | uncertainty domain commands, scope and resolution validation |
| backend_py/src/bfx_funding_bot/modules/execution/uncertainty_tables.py | SQLAlchemy submission_attempts and execution_uncertainties rows |
| backend_py/src/bfx_funding_bot/modules/api/uncertainties.py | operator read/bind/not-accepted/manual-resolution endpoints |
| backend_py/tests/modules/execution/test_submit_outcomes.py | deterministic HTTP/fault outcome classification tests |
| backend_py/tests/modules/execution/test_command_gate.py | ordering, block and one-attempt tests |
| backend_py/tests/modules/execution/test_uncertainties.py | scope, amount and event-only resolution tests |
| backend_py/tests/integration/test_unknown_submit_pg.py | crash/order/concurrency uncertainty integration |
| backend_py/tests/integration/test_orphan_quarantine_pg.py | full-account orphan persistence and scope tests |
| frontend/src/features/dashboard/__tests__/uncertainty-contract.test.ts | API DTO contract fixture |
| backend_py/alembic/versions/cd5e6f708192_add_submission_uncertainty.py | attempts and uncertainty schema |

### Modified files

| File | Responsibility after this plan |
|---|---|
| backend_py/src/bfx_funding_bot/modules/execution/protocols.py | typed SubmitOutcome and SubmittedOrder outcome field |
| backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py | classify response/transport without collapsing unknown into failed |
| backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py | persist attempt/outcome and uncertainty before continuing |
| backend_py/src/bfx_funding_bot/modules/execution/events.py | submit unknown, quarantine, bind and resolution events |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py | uncertainty and entity projections in serialized writer |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py | uncertain_amount and uncertainty projection link |
| backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py | convert unresolved PENDING to UNKNOWN; no auto retry |
| backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py | active/history query and exact normalized fields |
| backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py | match/adopt/quarantine logic after fresh full-account fence |
| backend_py/src/bfx_funding_bot/modules/execution/safety/chain.py | uncertainty block evaluated before economic guards |
| backend_py/src/bfx_funding_bot/modules/execution/safety/hard_guards.py | account/symbol uncertainty guard and fail-closed read behavior |
| backend_py/src/bfx_funding_bot/main.py | include uncertainty router with operator auth and account scope |
| frontend/src/types/index.ts | uncertainty DTO and submit outcome values |
| frontend/src/lib/query-keys.ts | account-scoped uncertainty key |
| frontend/src/features/dashboard/hooks/use-uncertainties.ts | uncertainty query and refetch after reconcile |
| frontend/src/features/dashboard/components/uncertainty-banner.tsx | symbol-scoped blocked/unknown UI |
| frontend/src/features/dashboard/components/uncertainty-details.tsx | evidence and operator action UI |
| frontend/src/app/[locale]/(dashboard)/overview/page.tsx | render uncertainty banner without hiding other symbols |
| frontend/e2e/uncertainty.spec.ts | operator UI resolution contract |
| backend_py/ARCHITECTURE.md | records UNKNOWN and orphan invariants |

## Task 1: Freeze typed outcomes and immutable attempt identity

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/execution/submit_outcomes.py
- Create: backend_py/tests/modules/execution/test_submit_outcomes.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/protocols.py
- Modify: backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py

**Interfaces:**
- SubmitOutcome is a discriminated union with SubmitAcknowledged(venue_offer_id, raw_response), SubmitRejected(reason, raw_response), SubmitOutcomeUnknown(reason, transport_started, raw_response_digest), and SubmitNotSent(reason).
- classify_submit_response(http_status, parsed_body, transport_started, exception) returns one union member; it defaults to UNKNOWN for unrecognized status/body.
- SubmissionAttemptPayload stores execution_decision_id, account UUID, environment, symbol, cid, normalized_payload, payload_sha256, started_at_ms, completed_at_ms, outcome_kind, outcome_reason, venue_offer_id and last_event_seq.
- SubmittedOrder exposes outcome_kind and preserves status compatibility only as a derived property; no UNKNOWN result is represented as status=failed.

- [ ] **Step 1: Write failing outcome tests**

Cover 2xx+ID acknowledged, structured Bitfinex rejection, allowlisted 4xx rejected, local validation not-sent, timeout/reset/5xx/malformed/cancel-after-start unknown, payload normalization and stable fingerprint.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/test_submit_outcomes.py tests/external/bitfinex/test_live_executor_pure.py -q

Expected: FAIL because transport errors currently return failed and no typed union exists.

- [ ] **Step 3: Implement classifier and executor mapping**

Keep request signing and payload shape unchanged. Capture only a digest of raw response for audit; truncate bounded diagnostics. Catch malformed JSON/shape after an HTTP response as UNKNOWN, not InvariantViolation that skips persistence.

- [ ] **Step 4: Run focused tests and static checks**

Run: cd backend_py && uv run pytest tests/modules/execution/test_submit_outcomes.py tests/external/bitfinex/test_live_executor_pure.py -q && uv run mypy src/bfx_funding_bot/modules/execution/submit_outcomes.py src/bfx_funding_bot/external/bitfinex/live_executor.py && uv run ruff check src/bfx_funding_bot/modules/execution/submit_outcomes.py src/bfx_funding_bot/external/bitfinex/live_executor.py

Expected: PASS.

- [ ] **Step 5: Commit typed outcomes**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/submit_outcomes.py backend_py/tests/modules/execution/test_submit_outcomes.py backend_py/src/bfx_funding_bot/modules/execution/protocols.py backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py
git commit -m "feat: classify submit outcomes explicitly"
~~~

## Task 2: Add attempts and uncertainty persistence

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/execution/uncertainty_tables.py
- Create: backend_py/src/bfx_funding_bot/modules/execution/uncertainties.py
- Create: backend_py/alembic/versions/cd5e6f708192_add_submission_uncertainty.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py
- Modify: backend_py/alembic/env.py
- Create: backend_py/tests/modules/execution/test_uncertainties.py

**Interfaces:**
- Revision cd5e6f708192 has down_revision = "bc4d5e6f7081"; creates submission_attempts with primary key attempt_id UUID, unique execution_decision_id, account/env/symbol/cid, normalized_payload JSONB, payload_sha256, typed outcome/reason, venue ID, timestamps and last_event_seq.
- execution_uncertainties has primary key uncertainty_id UUID and unique open scope (exchange_account_id, deployment_environment, symbol, kind); fields intended_amount, kind enum string, state open/resolved, evidence JSONB, opened/resolved event seq, resolved_by operator ID, resolution reason.
- UncertaintyService.open_or_get is idempotent and increments projected uncertain_amount only on first open; resolve requires matching scope, fresh reconcile event seq and operator ID.
- Supported kinds are submit_outcome_unknown, unattributed_venue_offer, unsupported_venue_exposure; unknown future kinds fail closed in the guard.

- [ ] **Step 1: Write failing table/domain tests**

Cover unique attempt per decision, immutable payload digest, idempotent open, amount non-negative, duplicate open not double counted, resolution freshness, and unknown kind rejection.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/test_uncertainties.py -q

Expected: FAIL because tables and service are absent.

- [ ] **Step 3: Implement metadata and migration**

Use PostgreSQL UUID/JSONB with sqlite-compatible type variants for unit fixtures. Add partial unique indexes for open scope and foreign keys with ON DELETE RESTRICT. Register all rows in alembic/env.py.

- [ ] **Step 4: Run migration drift and focused tests**

Run: cd backend_py && uv run pytest tests/modules/execution/test_uncertainties.py -q && uv run alembic check && uv run mypy src/bfx_funding_bot/modules/execution/uncertainties.py src/bfx_funding_bot/modules/execution/uncertainty_tables.py && uv run ruff check src/bfx_funding_bot/modules/execution/uncertainties.py src/bfx_funding_bot/modules/execution/uncertainty_tables.py

Expected: PASS.

- [ ] **Step 5: Commit persistence**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/uncertainty_tables.py backend_py/src/bfx_funding_bot/modules/execution/uncertainties.py backend_py/alembic/versions/cd5e6f708192_add_submission_uncertainty.py backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py backend_py/alembic/env.py backend_py/tests/modules/execution/test_uncertainties.py
git commit -m "feat: persist submission uncertainties"
~~~

## Task 3: Enforce command gate and middleware outcome durability

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/execution/command_gate.py
- Create: backend_py/tests/modules/execution/test_command_gate.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/events.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py

**Interfaces:**
- AccountCommandGate.check(decision, context) reads open uncertainty for exact account/env/symbol and fails closed on DB error; it runs before any new intent.
- AccountCommandGate.submit performs guard evaluation, writes ReservationIntent + SubmissionAttempt, releases the transaction, invokes executor, then writes exactly one typed outcome event and updates attempt; no database transaction spans network.
- UNKNOWN appends SubmitOutcomeUnknown with intended amount and opens uncertainty before returning; no bus publish or retry occurs. NOT_SENT appends ReservationFailed with reason local_pre_transport. ACK appends ReservationClaimed only after durable outcome. REJECT appends ReservationFailed.
- A process crash after intent but before outcome is represented as PENDING and is converted by boot recovery to UNKNOWN with uncertainty, never re-submitted.

- [ ] **Step 1: Write failing ordering/fault tests**

Use fake persister/venue to assert no venue call when uncertainty is open, no next submit before unknown commit, one attempt per decision, crash after intent opens uncertainty at recovery, and event/persistence failure blocks the command.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/test_command_gate.py tests/integration/test_reservation_write_path.py -q

Expected: FAIL because middleware has only submitted/failed branches and no uncertainty gate.

- [ ] **Step 3: Implement gate and middleware branches**

Move cid/payload normalization into the gate, preserve existing reservation reference identity, and call the executor only with ReadyToSubmit. Persist all attempt/outcome fields through the serialized AccountEventWriter.

- [ ] **Step 4: Run focused unit/fault tests**

Run: cd backend_py && uv run pytest tests/modules/execution/test_command_gate.py tests/integration/test_reservation_write_path.py tests/modules/execution/deployment/test_submit_attempt.py -q

Expected: PASS.

- [ ] **Step 5: Commit the command boundary**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/command_gate.py backend_py/tests/modules/execution/test_command_gate.py backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py backend_py/src/bfx_funding_bot/modules/execution/events.py backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py
git commit -m "feat: make unknown submit outcomes fail closed"
~~~

## Task 4: Reconcile unknown attempts and quarantine orphan venue objects

**Files:**
- Modify: backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/events.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py
- Create: backend_py/tests/integration/test_unknown_submit_pg.py
- Create: backend_py/tests/integration/test_orphan_quarantine_pg.py

**Interfaces:**
- Auth REST exposes active offers, active credits and paginated funding-offer history with venue ID, symbol, mts_created, amount_original, remaining amount, rate, period, status and coverage fence.
- match_unknown_attempt(attempt, active, history, coverage) -> MatchResult returns exact one match, zero_match, or multiple_match; only exact one emits SUBMIT_MATCHED_TO_VENUE_OFFER and resolves uncertainty.
- quarantine_orphan_offer(observation) -> VenueOfferQuarantined persists/counts an unmatched active offer and opens unattributed_venue_offer uncertainty; it never invents cid/ref/strategy and does not auto-cancel.
- PENDING recovery always emits SubmitOutcomeUnknown before any matching attempt. A fresh reconcile is required before operator adoption or not-accepted resolution.
- Entity terminal transitions are monotonic; a later old event cannot reopen a quarantined/closed venue object.

- [ ] **Step 1: Write failing fault and match tests**

Cover venue accept+drop, reject+drop, malformed/5xx after side effect, process restart, zero/multiple history candidates, incomplete history coverage, unknown symbols, orphan plus known offer, and cross-symbol continuation.

- [ ] **Step 2: Run integration tests and verify failure**

Run: cd backend_py && uv run pytest tests/integration/test_unknown_submit_pg.py tests/integration/test_orphan_quarantine_pg.py -m integration -q

Expected: FAIL because history matching and quarantine events do not exist.

- [ ] **Step 3: Implement reconcile evidence and events**

Keep active/history network calls outside writer transactions. Persist query start/end and coverage metadata in the snapshot event; use exact Decimal comparisons and venue timestamps. Do not infer rejection from an empty active list.

- [ ] **Step 4: Run deterministic fault suite**

Run: cd backend_py && uv run pytest tests/integration/test_unknown_submit_pg.py tests/integration/test_orphan_quarantine_pg.py -m integration -q

Expected: PASS; each fault leaves one durable attempt and a bounded uncertainty, and other symbols remain eligible.

- [ ] **Step 5: Commit UNKNOWN reconciliation**

~~~bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py backend_py/src/bfx_funding_bot/modules/execution/events.py backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py backend_py/tests/integration/test_unknown_submit_pg.py backend_py/tests/integration/test_orphan_quarantine_pg.py
git commit -m "feat: reconcile unknown submits and quarantine orphans"
~~~

## Task 5: Add operator resolution API and symbol-scoped guard/UI

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/api/uncertainties.py
- Modify: backend_py/src/bfx_funding_bot/main.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/safety/chain.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/safety/hard_guards.py
- Create: backend_py/tests/modules/api/test_uncertainties_router.py
- Modify: frontend/src/types/index.ts
- Modify: frontend/src/lib/query-keys.ts
- Create: frontend/src/features/dashboard/hooks/use-uncertainties.ts
- Create: frontend/src/features/dashboard/components/uncertainty-banner.tsx
- Create: frontend/src/features/dashboard/components/uncertainty-details.tsx
- Create: frontend/src/features/dashboard/__tests__/uncertainty-contract.test.ts
- Modify: frontend/src/app/[locale]/(dashboard)/overview/page.tsx
- Create: frontend/e2e/uncertainty.spec.ts

**Interfaces:**
- GET account-scoped uncertainties returns kind, symbol, intended amount, state, opened/resolved event seq, evidence summary and blocked scope; it never returns raw credentials or full payloads.
- POST bind-to-venue requires fresh reconcile fence and exact venue offer ID; POST mark-not-accepted requires fresh zero-candidate evidence; POST manual-resolution requires an explicit reason and operator UUID. All append resolution events.
- Uncertainty guard runs before economic sizing and blocks only matching account/env/symbol; DB errors and unsupported kinds block. UI shows banner per symbol and keeps unrelated positions/offers visible.
- React Query invalidates uncertainties and projections after each resolution/reconcile action; no UI action retries a venue submit.

- [ ] **Step 1: Write failing API/auth/UI tests**

Cover cross-account 404, stale fence rejection, missing reason rejection, successful bind/not-accepted/manual event, guard DB failure, and UI rendering of one blocked symbol while another remains active.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/api/test_uncertainties_router.py -q; then cd frontend && pnpm test -- uncertainty

Expected: FAIL because the router, guard and UI do not exist.

- [ ] **Step 3: Implement operator API and fail-closed guard**

Compose existing operator and account membership dependencies; return non-enumerating 404. Resolve only through the event writer after checking a fresh full-account reconcile fence. Keep metrics labels bounded to kind/state/outcome.

- [ ] **Step 4: Implement dashboard uncertainty surface**

Add typed DTOs and a compact banner/details component. Display intended amount, scope, evidence timestamp and next operator action; never display secrets or allow automatic retry.

- [ ] **Step 5: Run backend/frontend tests and quality checks**

Run: cd backend_py && uv run pytest tests/modules/api/test_uncertainties_router.py tests/modules/execution/test_command_gate.py -q && cd frontend && pnpm test -- uncertainty && pnpm lint

Expected: PASS.

- [ ] **Step 6: Commit resolution surface**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/api/uncertainties.py backend_py/src/bfx_funding_bot/main.py backend_py/src/bfx_funding_bot/modules/execution/safety/chain.py backend_py/src/bfx_funding_bot/modules/execution/safety/hard_guards.py backend_py/tests/modules/api/test_uncertainties_router.py frontend/src/types/index.ts frontend/src/lib/query-keys.ts frontend/src/features/dashboard/hooks/use-uncertainties.ts frontend/src/features/dashboard/components/uncertainty-banner.tsx frontend/src/features/dashboard/components/uncertainty-details.tsx 'frontend/src/app/[locale]/(dashboard)/overview/page.tsx' frontend/src/features/dashboard/__tests__/uncertainty-contract.test.ts frontend/e2e/uncertainty.spec.ts
git commit -m "feat: expose symbol scoped uncertainty resolution"
~~~

## Definition of Done

- Every submit has one immutable attempt with exact payload fingerprint and typed terminal/unknown result.
- Any ambiguous transport path becomes UNKNOWN, blocks only its account/environment/symbol, and counts intended amount pessimistically.
- Reconcile can adopt only one exact venue match; zero/multiple/incomplete evidence remains unresolved.
- Orphan venue offers/unsupported credits are durable, counted and quarantined without synthetic identity or auto-cancel.
- Resolution is operator-authenticated, fresh-evidence-gated and event-only; UI exposes scope and action without retry behavior.
- cd backend_py && uv run pytest -m "not integration" -q, deterministic integration fault tests, uv run alembic check, uv run mypy src/, uv run ruff check, and frontend tests/lint pass.
