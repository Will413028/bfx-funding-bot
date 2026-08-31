# ExchangeAccount Identity and Halt 1 Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Halt 1 內把現行字串 realm 收斂成 immutable ExchangeAccount UUID identity，將 credentials/config/money tables 完整映射到 account，並讓 API、vault、daemon 都拒絕 implicit/default account。

**Architecture:** 新增 exchange_accounts、exchange_account_memberships、exchange_account_credentials、account_config_drafts 與 legacy_account_realm_map；先以 additive schema 建表與 nullable UUID columns，執行帶 manifest 的一次性資料轉換與 credential AAD re-encryption，再以 contract migration 加上 NOT NULL/FK/索引並移除舊 realm 讀取。API 透過 explicit path UUID + membership dependency；daemon 只接受 BFX_EXCHANGE_ACCOUNT_ID。

**Tech Stack:** Python 3.13、SQLAlchemy 2.0 async、Alembic、PostgreSQL、Pydantic、FastAPI、Next.js 16、TypeScript、pytest。

**Spec:** docs/superpowers/specs/2026-08-31-production-integrity-foundation-design.md（Halt 1）。

## Global Constraints

- 只有在 preflight、offsite encrypted backup、isolated restore、row counts、event head/hash、legacy realm mapping 與 fresh venue reconcile 均通過後，才允許執行 cutover script；本 plan 不替 operator 執行 production migration。
- Alembic revision 只負責 schema/constraint；credential 解密、重新以 exchange_account_id 作 AAD 加密、UUID backfill 必須由可重跑且具 dry-run 的 application migration 完成，不能在 migration module 讀取環境 secret。
- 新 code 自第一個 cutover image 起只讀新欄位；舊 account_id 僅在 migration、audit evidence 與 rollback image 內存在，不得新增 dual-read fallback。
- 舊 users、executions、billing_records 只有 preflight 確認零 rows 才能移除；任何非零資料都停止 cutover 並保留 legacy table。
- Credential row 不可 hard delete；active credential 必須有 partial unique constraint，rotation 由新 row + 狀態轉移表達。
- API authorization order 必須是 JWT operator → explicit UUID path → membership/lifecycle → handler；他人 account 回 404，不可回 403 造成 enumeration。
- 所有 money tables（event_log、offer_claims、position_state、reconcile_observation、execution_decisions、diagnostics、nav_peak、trading_halt、attribution_weekly、config_regime）完成 UUID backfill 後才可 NOT NULL + ON DELETE RESTRICT。
- 測試與 migration 指令一律從 backend_py/ 使用 uv run；不使用 MCP 直接寫 production SQL。

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| backend_py/src/bfx_funding_bot/modules/accounts/exchange_accounts.py | account/membership/credential/config domain service and scoped queries |
| backend_py/src/bfx_funding_bot/modules/accounts/identity_cutover.py | dry-run/apply/verify identity mapping and credential AAD re-encryption |
| backend_py/src/bfx_funding_bot/modules/api/account_scope.py | explicit UUID path parser and membership authorization dependency |
| backend_py/scripts/cutover_identity.py | operator-invoked Halt 1 CLI with preflight, manifest hash, dry-run and apply modes |
| backend_py/tests/modules/accounts/test_exchange_accounts.py | domain identity and membership tests |
| backend_py/tests/modules/accounts/test_identity_cutover.py | idempotency, AAD migration, mapping and zero-row safety tests |
| backend_py/tests/modules/api/test_account_scope.py | 404/403/non-enumeration scope tests |
| backend_py/tests/integration/test_exchange_account_migration.py | PostgreSQL UUID/FK/rollback migration contract |
| deploy/vm/identity-realm-map.example.json | redacted realm-to-account mapping shape; contains no credentials |
| docs/runbooks/halt-1-exchange-account-cutover.md | exact preflight, halt, apply, verify and resume checklist |
| backend_py/alembic/versions/8a1b2c3d4e5f_add_exchange_account_identity.py | additive identity schema and nullable UUID columns |
| backend_py/alembic/versions/9b2c3d4e5f6a_contract_exchange_account_identity.py | post-backfill constraints, FKs, indexes and legacy removal guards |

### Modified files

| File | Responsibility after this plan |
|---|---|
| backend_py/src/bfx_funding_bot/modules/accounts/tables.py | new ExchangeAccount, Membership, Credential, AccountConfigDraft ORM rows; legacy rows remain migration-only |
| backend_py/src/bfx_funding_bot/modules/accounts/vault.py | account-scoped credential lifecycle and account UUID AAD |
| backend_py/src/bfx_funding_bot/modules/accounts/config_service.py | account-scoped draft config, no applied-state reads |
| backend_py/src/bfx_funding_bot/modules/api/api_keys.py | /exchange-accounts/{exchange_account_id}/credentials routes |
| backend_py/src/bfx_funding_bot/modules/api/config.py | /exchange-accounts/{exchange_account_id}/config-draft routes |
| backend_py/src/bfx_funding_bot/modules/api/projections.py | explicit account-scoped positions/offers/executions routes |
| backend_py/src/bfx_funding_bot/modules/api/attribution.py | explicit account UUID filter |
| backend_py/src/bfx_funding_bot/modules/api/routers.py | account-scoped profile/control endpoints |
| backend_py/src/bfx_funding_bot/modules/api/schemas.py | UUID/account fields and non-enumerating response DTOs |
| backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py | mandatory UUID env, account row load, no default fallback |
| backend_py/src/bfx_funding_bot/modules/execution/protocols.py | AccountContext.account_id becomes UUID |
| backend_py/src/bfx_funding_bot/core/crypto.py | explicit string canonicalization for UUID AAD |
| backend_py/alembic/env.py | imports identity tables and both revisions metadata |
| frontend/src/lib/api-client.ts | account scope helper and no implicit API realm |
| frontend/src/lib/query-keys.ts | account UUID is included in every private query key |
| frontend/src/features/api-keys/hooks/use-api-keys.ts | account-scoped credential paths |
| frontend/src/features/strategy/hooks/use-config.ts | account-scoped draft paths |
| frontend/src/features/dashboard/hooks/use-positions.ts | account-scoped projection path |
| frontend/src/features/dashboard/hooks/use-offers.ts | account-scoped projection path |
| frontend/src/features/dashboard/hooks/use-execution-events.ts | account-scoped cursor path |
| frontend/src/features/attribution/hooks/use-weekly-attribution.ts | account-scoped attribution path |
| frontend/e2e/smoke.spec.ts | supplies an explicit seeded account UUID |
| backend_py/ARCHITECTURE.md | records identity, membership, AAD and migration invariants |

## Task 1: Define the identity domain and ORM schema

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/accounts/exchange_accounts.py
- Create: backend_py/tests/modules/accounts/test_exchange_accounts.py
- Modify: backend_py/src/bfx_funding_bot/modules/accounts/tables.py

**Interfaces:**
- ExchangeAccount.id: UUID, venue: str, label: str, lifecycle_status: Literal["active","halted","retired"], created_at, retired_at; ID is immutable and hard delete is not exposed.
- ExchangeAccountMembership(exchange_account_id: UUID, user_id: str, role: Literal["owner","operator","viewer"]) uses composite primary key and unique (exchange_account_id,user_id).
- ExchangeAccountCredential.id: UUID, exchange_account_id: UUID, venue: str, label, encrypted envelope fields, key_version, lifecycle_status: Literal["active","revoked","retired"], verification timestamps; at most one active credential per account/venue.
- AccountConfigDraft.id: UUID, exchange_account_id: UUID, config: dict[str, object], revision: int, source: str, created/updated timestamps; source is user_configs for migrated rows and no daemon reader is exported.
- account_id_canonical(value: UUID | str) -> str returns lowercase hyphenated UUID and is the only AAD input for new credentials.

- [ ] **Step 1: Write failing model/domain tests**

Cover immutable ID assignment, role validation, one active credential invariant, config revision increment, UUID canonical AAD, and retired account rejection for new commands.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/accounts/test_exchange_accounts.py -q

Expected: FAIL because the new ORM rows and services do not exist.

- [ ] **Step 3: Implement rows and pure domain checks**

Add the rows without removing legacy models. Keep cross-schema FKs migration-owned so sqlite unit fixtures remain usable; service methods must accept AsyncSession and explicit UUID. Raise AccountNotFound, AccountRetired, MembershipDenied, and ActiveCredentialConflict domain errors.

- [ ] **Step 4: Run focused tests, mypy and ruff**

Run: cd backend_py && uv run pytest tests/modules/accounts/test_exchange_accounts.py -q && uv run mypy src/bfx_funding_bot/modules/accounts && uv run ruff check src/bfx_funding_bot/modules/accounts

Expected: PASS.

- [ ] **Step 5: Commit the identity domain**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/accounts/tables.py backend_py/src/bfx_funding_bot/modules/accounts/exchange_accounts.py backend_py/tests/modules/accounts/test_exchange_accounts.py
git commit -m "feat: add exchange account identity domain"
~~~

## Task 2: Add the additive Halt 1 migration

**Files:**
- Create: backend_py/alembic/versions/8a1b2c3d4e5f_add_exchange_account_identity.py
- Modify: backend_py/alembic/env.py
- Create: backend_py/tests/modules/accounts/test_identity_migration.py
- Create: backend_py/tests/integration/test_exchange_account_migration.py

**Interfaces:**
- Revision 8a1b2c3d4e5f has down_revision = "f5b8d0e2f3c4" and creates the four identity tables plus legacy_account_realm_map(realm_key, exchange_account_id, source, manifest_sha256).
- Add nullable exchange_account_id UUID to each money table and to api_keys/user_configs; preserve old columns until Task 6 contract migration.
- Add indexes on (exchange_account_id, deployment_environment) and unique constraints only where existing duplicate preflight proves safe; no silent deduplication.
- Downgrade drops only newly-added objects and is refused when any identity row has dependent data.

- [ ] **Step 1: Write failing migration metadata tests**

Assert table names, UUID column types, no ON DELETE CASCADE on money FKs, revision linkage, and downgrade refusal when dependencies exist.

- [ ] **Step 2: Run migration tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/accounts/test_identity_migration.py tests/integration/test_exchange_account_migration.py -m integration -q

Expected: FAIL because revision and identity metadata are absent.

- [ ] **Step 3: Implement additive Alembic revision**

Use Alembic operations and PostgreSQL UUID types; do not execute credential decrypt/re-encrypt in the revision. Register every new model in alembic/env.py so alembic check sees the same metadata.

- [ ] **Step 4: Verify migration locally**

Run: cd backend_py && uv run alembic check && uv run pytest tests/modules/accounts/test_identity_migration.py -q

Expected: PASS; integration test is skipped only when TEST_DATABASE_URL is unavailable.

- [ ] **Step 5: Commit the additive migration**

~~~bash
git add backend_py/alembic/versions/8a1b2c3d4e5f_add_exchange_account_identity.py backend_py/alembic/env.py backend_py/tests/modules/accounts/test_identity_migration.py backend_py/tests/integration/test_exchange_account_migration.py
git commit -m "feat: add exchange account cutover schema"
~~~

## Task 3: Implement idempotent realm mapping and credential/config migration

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/accounts/identity_cutover.py
- Create: backend_py/scripts/cutover_identity.py
- Create: backend_py/tests/modules/accounts/test_identity_cutover.py
- Modify: backend_py/src/bfx_funding_bot/modules/accounts/vault.py
- Modify: backend_py/src/bfx_funding_bot/modules/accounts/config_service.py
- Modify: backend_py/src/bfx_funding_bot/core/crypto.py

**Interfaces:**
- IdentityCutover.preflight(session, manifest) -> CutoverReport reports legacy realm counts, unmapped rows, duplicate active credentials, zero-row legacy candidates, event head/hash and mapping manifest SHA-256 without writes.
- IdentityCutover.apply(session, manifest, dry_run: bool) -> CutoverReport inserts deterministic UUIDs from the manifest, backfills every nullable UUID, migrates api_keys to exchange_account_credentials, creates account_config_drafts with source=user_configs, and is safe to run twice.
- IdentityCutover.verify(session, manifest) -> CutoverReport requires zero null UUIDs, exact source/target counts, decrypt/re-encrypt sample plus full credential verification, FK-ready rows, and no old implicit realm reference.
- Re-encryption decrypts each legacy api_keys.secret_* using AAD user_id, then encrypts the same secret with AAD str(exchange_account_id); plaintext never enters logs, report JSON, or database columns.
- Existing user config is explicitly a draft; no applied_version is inferred from its JSON.

- [ ] **Step 1: Write failing idempotency and AAD tests**

Use a fake KEK and two legacy rows to assert dry-run has no writes, apply twice has equal row counts, wrong manifest fails before writes, old AAD decrypts before conversion and fails after conversion, and a partial failed transaction rolls back all UUID backfills.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/accounts/test_identity_cutover.py -q

Expected: FAIL because cutover service and account-scoped vault functions do not exist.

- [ ] **Step 3: Implement cutover as one transaction per phase**

Validate manifest before opening a write transaction; use SELECT FOR UPDATE for legacy rows; flush all target rows; never delete legacy rows in this task. Make apply(dry_run=True) use rollback even when no exception occurs, and emit only aggregate counts/hashes.

- [ ] **Step 4: Move vault/config services to account identity**

Change create/list/delete/verify APIs to accept exchange_account_id and membership-checked caller context. Preserve response masking. AccountContext.account_id is the UUID string from the target row, never a credential row ID.

- [ ] **Step 5: Run focused tests and quality checks**

Run: cd backend_py && uv run pytest tests/modules/accounts/test_identity_cutover.py tests/test_api_key_model.py tests/test_api_keys_router.py tests/test_user_config_model.py -q && uv run mypy src/bfx_funding_bot/modules/accounts src/bfx_funding_bot/core/crypto.py && uv run ruff check src/bfx_funding_bot/modules/accounts src/bfx_funding_bot/core/crypto.py

Expected: PASS.

- [ ] **Step 6: Commit the data-migration tooling**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/accounts/identity_cutover.py backend_py/scripts/cutover_identity.py backend_py/src/bfx_funding_bot/modules/accounts/vault.py backend_py/src/bfx_funding_bot/modules/accounts/config_service.py backend_py/src/bfx_funding_bot/core/crypto.py backend_py/tests/modules/accounts/test_identity_cutover.py
git commit -m "feat: add idempotent account identity cutover"
~~~

## Task 4: Enforce explicit account authorization and API paths

**Files:**
- Create: backend_py/src/bfx_funding_bot/modules/api/account_scope.py
- Create: backend_py/tests/modules/api/test_account_scope.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/api_keys.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/config.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/projections.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/attribution.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/routers.py
- Modify: backend_py/src/bfx_funding_bot/modules/api/schemas.py

**Interfaces:**
- async def require_account_member(exchange_account_id: UUID, user: Principal = Depends(require_operator), session: AsyncSession = Depends(get_session)) -> ExchangeAccountContext checks active account and membership role; missing account or membership always raises 404 not_found.
- Canonical routes are /api/v1/exchange-accounts/{exchange_account_id}/positions, /offers, /executions, /credentials, /config-draft, and /attribution/weekly; old unscoped routes are removed in the contract migration, not aliased.
- All queries filter exchange_account_id and deployment_environment from the same dependency context; no module-level os.environ.get("BFX_ACCOUNT_ID", "default") remains.

- [ ] **Step 1: Write failing authorization/path tests**

Assert operator without membership gets non-enumerating 404, viewer cannot mutate credential/config, retired account returns 404 for command routes, valid owner can read/write permitted paths, and request without a UUID path never falls back to environment scope.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/api/test_account_scope.py tests/test_api_keys_router.py tests/test_config_router.py tests/test_projections_router.py -q

Expected: FAIL because all routes are still user-scoped or realm-global.

- [ ] **Step 3: Implement dependency and route migration**

Use UUID path parameters, dependency-returned context, and parameterized queries. Keep public router unchanged. Return 404 for both nonmembership and nonexistent account. Ensure rate limiting sees the operator principal but does not decide membership.

- [ ] **Step 4: Update frontend callers and query keys**

Add one explicit account selector sourced from the operator bootstrap response; every React Query key includes the UUID. Update hooks/actions and tests to assert exact /exchange-accounts/{id}/... paths; remove any BFX_ACCOUNT_ID query/default logic.

- [ ] **Step 5: Run backend/frontend focused tests**

Run: cd backend_py && uv run pytest tests/modules/api/test_account_scope.py tests/test_api_keys_router.py tests/test_config_router.py tests/test_projections_router.py -q; then cd frontend && pnpm test -- --runInBand

Expected: PASS.

- [ ] **Step 6: Commit explicit account scope**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/api/account_scope.py backend_py/src/bfx_funding_bot/modules/api/api_keys.py backend_py/src/bfx_funding_bot/modules/api/config.py backend_py/src/bfx_funding_bot/modules/api/projections.py backend_py/src/bfx_funding_bot/modules/api/attribution.py backend_py/src/bfx_funding_bot/modules/api/routers.py backend_py/src/bfx_funding_bot/modules/api/schemas.py backend_py/tests/modules/api/test_account_scope.py frontend/src/lib/api-client.ts frontend/src/lib/query-keys.ts frontend/src/features/api-keys/hooks/use-api-keys.ts frontend/src/features/strategy/hooks/use-config.ts frontend/src/features/dashboard/hooks/use-positions.ts frontend/src/features/dashboard/hooks/use-offers.ts frontend/src/features/dashboard/hooks/use-execution-events.ts frontend/src/features/attribution/hooks/use-weekly-attribution.ts frontend/e2e/smoke.spec.ts
git commit -m "refactor: require explicit exchange account scope"
~~~

## Task 5: Cut daemon over to immutable UUID identity

**Files:**
- Modify: backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
- Modify: backend_py/src/bfx_funding_bot/modules/execution/protocols.py
- Create: backend_py/tests/modules/marketfeed/test_account_bootstrap.py

**Interfaces:**
- _require_env("BFX_EXCHANGE_ACCOUNT_ID") parses a UUID and raises ConfigurationError for missing/invalid values; there is no default fallback and BFX_ACCOUNT_ID is rejected as an unknown legacy variable in live/canary phases.
- Daemon startup loads the account row, active credential row, and config draft by UUID, checks lifecycle active, and constructs AccountContext(account_id=str(uuid), ...).
- Diagnostics, safety stores, event store, ledger, registry and reconcile services all receive the same canonical UUID and deployment environment from this bootstrap object.

- [ ] **Step 1: Write failing bootstrap tests**

Cover missing env, malformed UUID, retired account, absent active credential, and successful context propagation to every constructed service.

- [ ] **Step 2: Run tests and verify failure**

Run: cd backend_py && uv run pytest tests/modules/marketfeed/test_account_bootstrap.py -q

Expected: FAIL because daemon still reads BFX_ACCOUNT_ID and constructs credentials directly from env.

- [ ] **Step 3: Implement bootstrap and remove implicit realm reads**

Load decrypted secret only through the account-scoped vault service. Pass one AccountBootstrap object through constructors; do not duplicate UUID parsing in individual modules.

- [ ] **Step 4: Run daemon-focused quality gates**

Run: cd backend_py && uv run pytest tests/modules/marketfeed/test_account_bootstrap.py tests/modules/execution -m "not integration" -q && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py src/bfx_funding_bot/modules/execution/protocols.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/daemon.py src/bfx_funding_bot/modules/execution/protocols.py

Expected: PASS.

- [ ] **Step 5: Commit daemon identity cutover**

~~~bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/src/bfx_funding_bot/modules/execution/protocols.py backend_py/tests/modules/marketfeed/test_account_bootstrap.py
git commit -m "refactor: bootstrap daemon from exchange account uuid"
~~~

## Task 6: Contract migration and Halt 1 runbook

**Files:**
- Create: backend_py/alembic/versions/9b2c3d4e5f6a_contract_exchange_account_identity.py
- Create: docs/runbooks/halt-1-exchange-account-cutover.md
- Create: deploy/vm/identity-realm-map.example.json
- Modify: backend_py/tests/integration/test_exchange_account_migration.py
- Modify: backend_py/ARCHITECTURE.md

**Interfaces:**
- Revision 9b2c3d4e5f6a has down_revision = "8a1b2c3d4e5f"; it refuses to run if any money-table UUID is null, any legacy realm is unmapped, or users/executions/billing_records contain rows.
- On success it adds NOT NULL and FOREIGN KEY ... ON DELETE RESTRICT, recreates affected composite keys/indexes with UUID, and drops only proven-zero legacy tables; application code removes old unscoped API assumptions before this revision runs.
- Runbook order is fixed: enumerate realms → freeze writes → backup + isolated restore → event count/head/hash → fresh full-account venue reconcile → persist halt → stop worker → alembic upgrade head → cutover_identity.py --dry-run → apply → verify → contract migration → replay/auth checks → one-account canary gate.

- [ ] **Step 1: Write failing contract-migration tests**

Test null UUID, unmapped realm, nonzero legacy table, missing event hash, and valid migration; assert every failure is before destructive DDL and transaction rolls back.

- [ ] **Step 2: Run integration tests and verify failure**

Run: cd backend_py && uv run pytest tests/integration/test_exchange_account_migration.py -m integration -q

Expected: FAIL because contract revision and runbook preconditions are absent.

- [ ] **Step 3: Implement guarded contract revision**

Use transaction-safe PostgreSQL DDL; encode preflight checks in the revision and repeat them in the CLI. Do not drop legacy rows or tables when a check fails.

- [ ] **Step 4: Complete documentation and drift checks**

Record identity columns, FK policy, membership roles, AAD rule, API paths, and rollback boundary in backend_py/ARCHITECTURE.md; run cd backend_py && uv run alembic check.

- [ ] **Step 5: Commit Halt 1 artifacts**

~~~bash
git add backend_py/alembic/versions/9b2c3d4e5f6a_contract_exchange_account_identity.py backend_py/tests/integration/test_exchange_account_migration.py backend_py/ARCHITECTURE.md docs/runbooks/halt-1-exchange-account-cutover.md deploy/vm/identity-realm-map.example.json
git commit -m "feat: complete halt one account identity cutover"
~~~

## Definition of Done

- Every money row has a validated immutable exchange_account_id UUID; no daemon or API path relies on BFX_ACCOUNT_ID or an implicit default.
- Credentials are account-scoped, multi-row lifecycle records, and all migrated secrets decrypt only with account UUID AAD.
- Config is explicitly a draft and is not read as applied daemon state.
- Membership and role checks are explicit, non-enumerating, and tested across every private route.
- Halt 1 preflight/apply/verify is idempotent, auditable, transaction-safe and documented; zero-row legacy deletion is guarded.
- cd backend_py && uv run pytest -m "not integration" -q, integration migration tests with PostgreSQL, uv run alembic check, uv run mypy src/, uv run ruff check, and frontend tests pass.
