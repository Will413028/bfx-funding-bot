# Serialized Execution Projector and Entity Projections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 event_log 變成每個 account/environment 的唯一 ordering boundary，使用 transaction-scoped advisory lock 及 deterministic projector 維護 position、offer、credit 與 projection cursor，消除 concurrent RMW、direct snapshot mutation 與全域 lag 誤判。

**Architecture:** AccountEventWriter 在一個 transaction 內取得 (exchange_account_id, deployment_environment) advisory xact lock、append event、依 event_seq 順序投影 entity state、更新 projection_heads 後 commit。venue network 永不在該 transaction 內。歷史 v2 event 由 immutable row fields 產生 UUIDv5；新 v3 event 帶 UUIDv4 event_id 並以 (account,env,event_id) partial unique 約束去重。reconcile 以 full-account VENUE_SNAPSHOT_OBSERVED event 表達，不再呼叫直接覆寫 position state 的 API。

**Tech Stack:** Python 3.13、asyncio、SQLAlchemy 2.0 async、PostgreSQL advisory locks、Alembic、Pydantic/dataclasses、pytest、asyncpg。

**Spec:** docs/superpowers/specs/2026-08-31-production-integrity-foundation-design.md（serialized projector）。

## Global Constraints

- Projection transaction 不可包含 HTTP/WebSocket/Bitfinex network call；network result 必須先被 normalize 成 event，再交給 writer。
- transaction lock key 一律由 canonical ExchangeAccount UUID + deployment environment 經與 daemon WriterLock 相同輸入、但不同 namespace 的 derive_transaction_lock_key 產生；不得只鎖 account、只鎖 process 或依 symbol 分鎖。
- last_event_seq、projection_heads.last_event_seq 只能由 projector 寫入；任何 API、reconcile 或 ledger callback 不得直接改 position projection。
- projector 對 FSM invalid transition 必須 rollback 整個 transaction、寫 bounded alert/log，不能 silently coerce 或把 terminal object reopen。
- rebuild 只讀 event_log、upcaster 與 projector；不得讀既有 projection、wall clock、live venue API 或 mutable config。
- active/history venue query 必須涵蓋該 account 全部 funding offers/credits，不可只查 config symbols；未知 symbol 仍要持久化並進入 account/symbol exposure。
- gross_exposure = offered_amount + lent_amount + uncertain_amount；uncertain_amount 初始為 0，由 Plan 4 UNKNOWN flow 增加。
- migration 使用 Alembic；所有 DB concurrency tests 需真 PostgreSQL，sqlite 只測 pure projector/domain。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| backend_py/src/bfx_funding_bot/modules/execution/event_store/projector.py | pure v3 upcaster, FSM reducer, entity projection and deterministic rebuild |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/writer.py | transaction-owning advisory-lock AccountEventWriter |
| backend_py/alembic/versions/c2e3f4a5b6c7_seed_projection_heads.py | seed historical projection cursors before replay is enabled |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/entities.py | normalized venue offer/credit snapshot records |
| backend_py/tests/modules/execution/event_store/test_projector.py | pure transition/upcaster/rebuild tests |
| backend_py/tests/modules/execution/event_store/test_account_event_writer.py | transaction and lock unit tests |
| backend_py/tests/integration/test_serialized_projector_pg.py | concurrent same-account/cross-account/rollback tests |
| backend_py/alembic/versions/bc4d5e6f7081_add_serialized_projector.py | event identity, projection heads and venue entity tables |

### Modified files

| File | Responsibility after this plan |
|---|---|
| backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py | v3 event_id, projection_heads, venue_offer_state, venue_credit_state and expanded position fields |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py | delegates append/projection to AccountEventWriter/projector; removes direct RMW semantics |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/persister.py | exposes writer boundary and preserves one transaction per event batch |
| backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py | schema-v2 upcaster and schema-v3 event identity validation |
| backend_py/src/bfx_funding_bot/modules/execution/events.py | event_id, schema_version=3 and canonical UUID account field |
| backend_py/src/bfx_funding_bot/core/writer_lock.py | transaction-scoped advisory lock helper alongside daemon session lock |
| backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py | full-account offers/credits query and normalized snapshot support |
| backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py | emits full-account snapshot events and consumes entity projections |
| backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py | routes fresh full-account observations through writer |
| backend_py/src/bfx_funding_bot/modules/execution/registry_offers.py | terminal monotonic entity identity; no synthetic offer IDs |
| backend_py/src/bfx_funding_bot/modules/execution/ledger.py | consumes projected state without becoming a second database writer |
| backend_py/src/bfx_funding_bot/modules/api/projections.py | reads account-scoped projection rows and cursor lag |
| backend_py/src/bfx_funding_bot/modules/api/schemas.py | offered/lent/available/uncertain/gross and projection lag fields |
| backend_py/alembic/env.py | registers projector metadata |
| backend_py/ARCHITECTURE.md | records lock, event identity and deterministic rebuild invariants |

### Deleted files

| File | Reason |
|---|---|
| backend_py/tests/modules/execution/event_store/test_set_position_snapshot.py | direct snapshot mutation is replaced by VENUE_SNAPSHOT_OBSERVED event tests |

## Task 1: Freeze schema-v3 event identity and normalized entities

**Files:**
- Modify: backend_py/src/bfx_funding_bot/modules/execution/events.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py
- Create: backend_py/tests/modules/execution/event_store/test_projector.py

**Interfaces:**
- Every new event constructor accepts event_id: UUID (default generated at creation), canonical UUID account_id: str, and schema_version=3; serialization always emits event_id as lowercase string.
- StoredEventIdentity(event_id: UUID, source: Literal["native","derived_v2"]) is exposed by stored_event_identity(row); v2 rows derive UUIDv5 from namespace bfx-funding-bot/event-log/v2 plus immutable tuple (event_seq,account_id,env,event_type,occurred_at_ms,payload_json).
- EventLogRow.event_id is nullable for historical rows and required by a check for schema_version 3; the canonical partial unique index covers (exchange_account_id, deployment_environment, event_id), with a transitional legacy-account companion index for SQLite/fixture rows.
- PositionStateRow adds offered_amount, lent_amount, available_amount, uncertain_amount, last_venue_snapshot_at; the additive migration backfills them while the legacy reserved/realized columns remain read-compatible. After the Task 3/4 writer and account-observation cutover, a dedicated contract migration drops the old columns.
- VenueOfferStateRow primary key is (exchange_account_id, deployment_environment, venue_offer_id) and stores symbol, amount_original, amount_remaining, rate, period, status, first/last seen event seq, terminal flag.
- VenueCreditStateRow primary key is (exchange_account_id, deployment_environment, credit_id) and stores symbol, amount, rate, period, status, first/last seen event seq.

- [ ] **Step 1: Write failing identity/upcaster/entity tests**

Test UUID serialization, deterministic v2 derivation independent of process, v3 missing-ID rejection, exact account/env uniqueness, terminal monotonicity, and gross exposure arithmetic using Decimal.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_projector.py -q

Expected: FAIL because schema-v3 identity and entity rows do not exist.

- [ ] **Step 3: Implement identity and normalized records**

Keep historical decoder compatibility explicit in one upcaster; never mutate historical payloads. Make terminal status transitions a pure function that rejects reopening and invalid state edges.

- [ ] **Step 4: Run focused tests and static checks**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_projector.py -q && uv run mypy src/bfx_funding_bot/modules/execution/events.py src/bfx_funding_bot/modules/execution/event_store && uv run ruff check src/bfx_funding_bot/modules/execution/events.py src/bfx_funding_bot/modules/execution/event_store

Expected: PASS.

- [ ] **Step 5: Commit the v3 domain contract**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/execution/events.py backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py backend_py/tests/modules/execution/event_store/test_projector.py
git commit -m "feat: add schema v3 event identity contract"
~~~

## Task 2: Add the projector migration and metadata

**Files:**
- Create: backend_py/alembic/versions/bc4d5e6f7081_add_serialized_projector.py
- Modify: backend_py/alembic/env.py
- Modify: backend_py/tests/modules/execution/event_store/test_tables_metadata.py
- Create: backend_py/tests/integration/test_serialized_projector_pg.py

**Interfaces:**
- Revision bc4d5e6f7081 has down_revision = "9b2c3d4e5f6a"; creates projection_heads with primary key (exchange_account_id, deployment_environment, projection_name) and projector_version, and the two entity tables.
- It adds nullable event_id/schema_version columns and the partial unique index, then backfills historical event IDs only through the application upcaster during replay; it never rewrites immutable payloads.
- It adds position numeric columns with zero defaults and backfills them from the current canonical-compatible state. It intentionally retains reserved/realized until the Task 3/4 writer and full-account observation cutover; a follow-up contract migration drops those obsolete direct-mutation columns. All account FKs use ON DELETE RESTRICT.

- [ ] **Step 1: Write failing metadata/integration tests**

Assert revision linkage, constraints/indexes, no cascade deletes, event v3 check, and that a fresh empty projection schema can be created and dropped.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_tables_metadata.py tests/integration/test_serialized_projector_pg.py -m integration -q

Expected: FAIL because migration and metadata are absent.

- [ ] **Step 3: Implement Alembic migration**

Use transaction-safe additive DDL and register every mapped table. Keep event history append-only; no migration step may update payload JSON or delete event rows. Do not drop reserved/realized until the writer no longer writes them and the full-account observation tests prove the new buckets are authoritative.

- [ ] **Step 4: Verify Alembic drift**

Run: cd backend_py && uv run alembic check && uv run pytest tests/modules/execution/event_store/test_tables_metadata.py -q

Expected: PASS.

- [ ] **Step 5: Commit projector schema**

~~~bash
git add backend_py/alembic/versions/bc4d5e6f7081_add_serialized_projector.py backend_py/alembic/env.py backend_py/tests/modules/execution/event_store/test_tables_metadata.py backend_py/tests/integration/test_serialized_projector_pg.py
git commit -m "feat: add serialized projector schema"
~~~

## Task 3: Implement lock-owned append and projection cursor

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/execution/event_store/writer.py
- Modify: backend_py/src/bfx_funding_bot/core/writer_lock.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/persister.py
- Create: backend_py/tests/modules/execution/event_store/test_account_event_writer.py

**Interfaces:**
- AccountEventWriter.append(session, event) -> AppendResult executes pg_advisory_xact_lock(transaction_lock_key) before append; it does not commit and returns event seq, dedup status and projection head. The transaction key uses a namespace distinct from the daemon session lock so both guards can be held simultaneously.
- AccountEventWriter.append_batch(session, events) orders input by domain event time only when the caller explicitly supplies a batch; persisted ordering is always database event_seq, not wall-clock sort.
- Before appending, the writer replays account/environment event-log rows after `projection_heads.last_event_seq` in ascending `event_seq`, then projects the new row and advances the cursor in the same transaction. A duplicate older event leaves the cursor at its monotonic high-water mark.
- Revision `c2e3f4a5b6c7` seeds cursors for historical snapshots created before the serialized writer. The writer rejects an Alembic-managed database at an earlier revision; an explicit compatibility mode is available only to direct-create legacy fixtures.
- projection_heads is updated in the same transaction as event and projections. Lag query uses account/env filtered max(event_seq) minus head.last_event_seq; no global max is used.
- Existing daemon WriterLock remains the long-lived connection guard; the xact lock is an additional transaction serialization boundary and both must be held for live writes.

- [x] **Step 1: Write failing lock/rollback tests**

Cover same-account concurrent append serialization, cross-account non-blocking behavior, projection failure rollback of event and head, dedup behavior, and head lag scoped to account/env.

- [x] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_account_event_writer.py -q

Expected: FAIL because append has no transaction advisory lock or cursor.

- [x] **Step 3: Implement writer and projector invocation**

Move the event projection primitive behind the writer boundary; keep `store.append` as an explicit compatibility adapter while the production `EventStorePersister` defaults to strict canonical account identity. Set `OfferClaimRow.last_event_seq` to the actual inserted event seq and reject migrated databases until the historical cursor seed is installed.

- [x] **Step 4: Run unit and PostgreSQL concurrency tests**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_account_event_writer.py -q && uv run pytest tests/integration/test_serialized_projector_pg.py -m integration -q

Expected: PASS; 100 concurrent writes to one account produce a contiguous ordered head, while two accounts can progress independently.

- [x] **Step 5: Commit the writer boundary**

~~~bash
git add backend_py/alembic/versions/c2e3f4a5b6c7_seed_projection_heads.py backend_py/src/bfx_funding_bot/modules/execution/event_store/writer.py backend_py/src/bfx_funding_bot/core/writer_lock.py backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py backend_py/src/bfx_funding_bot/modules/execution/event_store/persister.py backend_py/tests/modules/execution/event_store/test_account_event_writer.py backend_py/tests/integration/test_serialized_projector_pg.py backend_py/tests/integration/test_writer_lock.py backend_py/tests/modules/execution/event_store/test_persister_dedup.py backend_py/tests/integration/test_boot_recovery_pg.py backend_py/tests/integration/test_daemon_pg_cutover.py backend_py/tests/integration/test_reservation_write_path.py backend_py/tests/external/bitfinex/test_source_persistence.py docs/superpowers/plans/2026-08-31-serialized-execution-projector.md backend_py/ARCHITECTURE.md
git commit -m "feat: serialize account event projection writes"
~~~

## Task 4: Replace direct snapshots with full-account observation events

**Files:**
- Modify: backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/ledger.py
- Delete: backend_py/tests/modules/execution/event_store/test_set_position_snapshot.py
- Create: backend_py/tests/modules/execution/event_store/test_full_account_snapshot.py

**Interfaces:**
- BitfinexAuthREST.get_active_funding_offers(ctx, symbol: str | None = None) and credits with symbol=None fetch every account object; response normalization retains venue IDs and all active/history fields.
- FullAccountSnapshot(account_id: UUID, environment: str, query_started_at_ms, query_finished_at_ms, offers: tuple[VenueOfferObservation,...], credits: tuple[VenueCreditObservation,...], wallet_available: Mapping[str, Decimal], coverage: SnapshotCoverage) is immutable and includes explicit endpoint coverage and pagination.
- VENUE_SNAPSHOT_OBSERVED projects entity rows and absolute position fields in one account transaction; it also appends immutable reconcile_observation audit data. set_position_snapshot() is deleted and no caller may directly mutate PositionStateRow.
- rebuild_snapshot_from_log(account_id, environment) creates empty projections, upcasts every event in event_seq order, applies observations and domain events, and verifies resulting head equals max account event seq.

- [ ] **Step 1: Write failing full-account and rebuild tests**

Assert no symbol filter is sent for full reconcile, unknown symbols are persisted, snapshot coverage metadata is required, direct set_position_snapshot import fails, and rebuilding two event-order permutations yields identical projections.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_full_account_snapshot.py tests/modules/execution/test_periodic_reconcile.py -q

Expected: FAIL because reconcile still writes per-symbol snapshots and direct mutation exists.

- [ ] **Step 3: Implement normalized full-account observation flow**

Keep all venue calls outside DB transactions; after query completion build one immutable snapshot event. Project offer/credit entities with object-level upsert and terminal monotonicity. Include all active offers/credits even when symbol is absent from configuration.

The same cutover owns the contract migration that drops `position_state.reserved` and `position_state.realized` after a verified backfill and a zero-legacy-writer architecture check.

- [ ] **Step 4: Run focused recovery/reconcile tests**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store/test_full_account_snapshot.py tests/modules/execution/test_periodic_reconcile.py tests/modules/execution/test_position_reconciled.py -q

Expected: PASS.

- [ ] **Step 5: Commit full-account reconciliation**

~~~bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py backend_py/src/bfx_funding_bot/modules/execution/ledger.py backend_py/tests/modules/execution/event_store/test_set_position_snapshot.py backend_py/tests/modules/execution/event_store/test_full_account_snapshot.py
git commit -m "refactor: project full account venue observations"
~~~

## Task 5: Expose projection health and deterministic replay

**Files:**
- Modify: backend_py/src/bfx_funding_bot/modules/api/projections.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/schemas.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/registry_offers.py
- Modify: backend_py/tests/test_projections_router.py
- Modify: backend_py/tests/modules/execution/event_store/test_rebuild_from_checkpoint.py
- Modify: backend_py/ARCHITECTURE.md

**Interfaces:**
- Account-scoped execution response includes projectionHead, eventHead, lag, projectorVersion; lag is computed within the requested account/environment only.
- OfferRegistry reads venue_offer_state and never creates synthetic IDs/ref/cid for unmatched venue objects; terminal rows cannot return to active.
- Replay command/test can target an empty projection schema and emits a content hash of resulting rows; same event log + projector version must produce the same hash.

- [ ] **Step 1: Write failing API/replay tests**

Assert account-scoped lag, terminal monotonicity after replay, empty projection rebuild, and stable content hash across two runs.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/test_projections_router.py tests/modules/execution/event_store/test_rebuild_from_checkpoint.py -q

Expected: FAIL because response and replay still use old columns/checkpoint path.

- [ ] **Step 3: Implement cursor API and replay verification**

Read only account-scoped rows; expose Decimal values as canonical strings. Make replay fail if an upcaster/projector version is missing or if event identity is invalid.

- [ ] **Step 4: Run all event-store tests and quality gates**

Run: cd backend_py && uv run pytest tests/modules/execution/event_store tests/modules/execution/test_events.py tests/test_projections_router.py -m "not integration" -q && uv run mypy src/bfx_funding_bot/modules/execution/event_store src/bfx_funding_bot/modules/api/projections.py && uv run ruff check src/bfx_funding_bot/modules/execution/event_store src/bfx_funding_bot/modules/api/projections.py

Expected: PASS.

- [ ] **Step 5: Commit projector observability and docs**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/api/projections.py backend_py/src/bfx_funding_bot/modules/api/schemas.py backend_py/src/bfx_funding_bot/modules/execution/registry_offers.py backend_py/tests/test_projections_router.py backend_py/tests/modules/execution/event_store/test_rebuild_from_checkpoint.py backend_py/ARCHITECTURE.md
git commit -m "docs: record serialized projector invariants"
~~~

## Definition of Done

- Same-account concurrent appends are serialized by transaction advisory lock; cross-account work remains independent; failures rollback event and projection together.
- New events have stable UUID identity; historical events replay through deterministic upcasters without mutating immutable rows.
- Position, offer, credit and projection head are account/env scoped and rebuildable from event_log alone.
- Full-account reconcile captures active offers and credits across all symbols; no direct position snapshot mutation remains.
- API reports account-local projection lag and replay hash.
- cd backend_py && uv run pytest -m "not integration" -q, PostgreSQL concurrency/replay integration tests, uv run alembic check, uv run mypy src/, and uv run ruff check pass.
