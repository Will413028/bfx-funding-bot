# Production Integrity Foundation Design

- **Date:** 2026-08-31
- **Status:** Approved design; pending written-spec review
- **Parent decision:** `2026-08-31-account-isolated-execution-target-architecture`
- **Scope:** P0 operator containment, canonical exchange-account identity, serialized execution projections, UNKNOWN submit handling, and venue-object quarantine
- **Operational constraint:** Planned trading halts are permitted; zero-downtime migration is not required

## 1. Executive summary

The target architecture is implemented as a program, not a single rewrite:

1. **Production integrity foundation** — this specification.
2. **Offsite disaster recovery** — encrypted WAL/PITR, restore automation, and monthly drills.
3. **Control/execution seam** — immutable config versions, desired/applied state, transactional outbox, one credential provider, and account-worker leases.
4. **Public/API/delivery hardening** — static public artifacts, generated contracts, immutable images, and expand/contract delivery.
5. **External real-money beta gates** — RLS/scoped roles, KMS rotation, economic non-inferiority, Bitfinex consent, and legal review.

This specification establishes the safety and identity foundation that every later workstream depends on. It makes `ExchangeAccount` the money-domain aggregate root, closes the public signup and authorization boundary, replaces concurrent delta mutation with an ordered account projector, and treats any ambiguous venue write as UNKNOWN rather than failed.

The selected delivery strategy is **staged clean cutovers**:

```text
Operator containment
        ↓
Verified offsite pre-cutover backup
        ↓
ExchangeAccount identity cutover       (Halt 1)
        ↓
Per-account serialized projector
        ↓
UNKNOWN lifecycle + orphan quarantine  (Halt 2)
        ↓
Projection rebuild + venue reconcile
        ↓
Bounded real-money canary
```

There is no runtime dual-read, legacy realm fallback, or automatic retry of an ambiguous Bitfinex funding-offer submission.

## 2. Context and current failure modes

The existing execution core already has several strong properties:

- durable write-ahead reservation intent before a venue submit;
- one live daemon writer protected by a PostgreSQL advisory lock;
- append-only `event_log` as the execution source of truth;
- REST reconcile as the correctness backbone;
- fail-closed safety guards and persistent trading halt;
- event-linked execution decisions and bounded real-money caps.

The remaining gaps cross those boundaries:

1. Better Auth signup is publicly reachable, while every authenticated user can reach account projections selected from process-global `BFX_ACCOUNT_ID` and `BFX_DEPLOYMENT_ENV`.
2. The daemon, web API, vault, and config CRUD disagree on what an account is. The daemon reads environment credentials and a string realm; the web surface owns vault/config rows by login user.
3. A transport error or HTTP 5xx during funding-offer submit is currently returned as `failed`, even though the venue may have accepted the request. Bitfinex funding offers do not accept a client `cid`, so a retry can create duplicate exposure.
4. `position_state` uses concurrent read-modify-write deltas without row or account serialization. Submit outcomes, WebSocket events, and reconcile snapshots can overwrite or double-apply one another.
5. Reconcile currently raises on an unattributed venue offer, aborting the rest of the snapshot instead of recording the object, counting its exposure, and isolating only the affected account/symbol.
6. `set_position_snapshot()` mutates a projection outside the ordered event stream. A REST snapshot and a concurrent event can therefore produce a live state that is not reproducible from the event log.

This work is performed while there is one operator and no external user money. External beta remains blocked by the parent ADR gates.

## 3. Options considered

### 3.1 Big-bang production rewrite

Ship authorization, identity, event model, projection tables, UNKNOWN handling, and quarantine in one maintenance window.

- Advantage: one final-state cutover.
- Cost: the migration, authentication, and venue-state blast radii become inseparable; rollback evidence is harder to interpret.

### 3.2 Staged clean cutovers — selected

Deploy operator containment first, then use one halt for canonical identity and a second halt for the execution-state model.

- Advantage: each cutover has one dominant failure domain and independent acceptance evidence.
- Advantage: planned halts avoid runtime dual-write and compatibility branches.
- Cost: two maintenance windows and two bounded canary decisions.

### 3.3 Patch execution under the legacy realm, migrate identity later

Add UNKNOWN and locks against the existing string `account_id`, then repeat the data work when `ExchangeAccount` arrives.

- Advantage: shortest path to the ambiguous-submit patch.
- Cost: temporary identity semantics, repeated migrations, and new tables that must later be rewritten. This conflicts with the no-debt objective.

## 4. Design principles and invariants

1. **Exchange account, not user, is the money aggregate.** Credential, nonce, exposure, reconcile, execution state, and blast radius belong to `ExchangeAccount`.
2. **Authorization is explicit and data-backed.** A JWT authenticates a person; a membership authorizes an account. Process environment never chooses a web request's account scope.
3. **No ambiguous failure.** Without explicit evidence that a venue write was not accepted, the result is UNKNOWN.
4. **No retry after ambiguity.** The same attempt is never resent, and the affected account/symbol is blocked until resolved.
5. **Venue state is the exposure authority.** Every active offer and credit is persisted and counted even when local attribution is missing.
6. **All materialized execution state follows one ordered account stream.** Event append, projection update, and cursor advancement commit atomically.
7. **Reconcile observations are events.** There is no projection-only absolute overwrite.
8. **Projectors are deterministic.** They do not read current time, process environment, live venue APIs, or prior projection values during a clean rebuild.
9. **Rollback never forgets venue writes.** After venue authority is restored, DB restore requires proof that no later venue mutation occurred; otherwise recovery is halt, reconcile, and forward-fix.

## 5. Canonical identity and authorization boundary

### 5.1 Domain model

#### `exchange_accounts`

The canonical aggregate root:

| Field | Contract |
|---|---|
| `id` | UUID primary key; immutable |
| `venue` | `bitfinex` for the current product |
| `label` | Operator-facing label; not an authorization key |
| `status` | `provisioning`, `active`, `suspended`, or `closed` |
| timestamps | creation and lifecycle audit |

An account is never hard-deleted after it has money events. Lifecycle changes use `status`.

#### `exchange_account_memberships`

Maps a Better Auth person to an exchange account:

| Field | Contract |
|---|---|
| `exchange_account_id` | FK to `exchange_accounts`, `ON DELETE RESTRICT` |
| `user_id` | Better Auth `auth.user.id` text identifier |
| `role` | `owner`, `operator`, or `viewer` |
| timestamps | grant/update audit |

The primary key is `(exchange_account_id, user_id)`. Current production contains exactly one operator membership, but the shape does not need to change for a future second account.

#### `exchange_account_credentials`

The existing user-owned `api_keys` row is migrated into account-owned credentials:

- multiple rows per account are allowed so API-key and encryption-key rotation do not require destructive overwrite;
- lifecycle is `pending`, `active`, `retired`, or `revoked`;
- at most one active Bitfinex execution credential exists per account;
- the encrypted secret uses `exchange_account_id` as authenticated encryption context;
- verification evidence and dangerous-scope checks remain fail-closed;
- the daemon continues using environment credentials until the later `CredentialProvider` workstream, but no new credential schema migration is required then.

Credential re-encryption is an audited, idempotent application data-migration command. Alembic owns schema changes; secrets are not embedded in a migration revision.

#### `account_config_drafts`

The inert user-owned `user_configs` row becomes an account-owned draft:

- it is explicitly not applied execution state;
- the UI may edit it without claiming the worker consumed it;
- the later control-plane workstream publishes a draft into an immutable config version and desired-state command.

`user_profiles` remains user-level product metadata. The unused `users`, `executions`, and `billing_records` scaffold is removed only when a cutover preflight proves all three tables are empty; otherwise the migration aborts for explicit classification.

### 5.2 Money-table migration

The following account columns become `exchange_account_id UUID NOT NULL` with a restrictive FK:

- `event_log`
- `offer_claims`
- `position_state`
- `reconcile_observation`
- `execution_decisions`
- `diagnostics`
- `nav_peak`
- `trading_halt`
- `attribution_weekly`
- `config_regime`

`deployment_environment` remains the runtime/event-stream dimension. It is not an account identity.

The cutover uses an explicit legacy-realm mapping manifest. Every distinct legacy realm is inspected. Historical and current Bitfinex accounts are mapped to separate `ExchangeAccount` rows even when an old deployment reused the same human label.

Historical event payloads remain immutable. Stored-event decoding takes canonical aggregate identity from `EventLogRow.exchange_account_id`; it does not trust a legacy payload's old realm string. New events carry the canonical UUID string in their domain `account_id` field for backward-compatible event contracts.

### 5.3 API authorization

Authentication and authorization are separate:

```text
Better Auth session
      ↓
BFF-minted short-lived JWT
      ↓
FastAPI signature/issuer/audience validation
      ↓
immutable principal sub + role
      ↓
membership lookup for requested ExchangeAccount
```

After Halt 1, current production policy requires both:

- `sub == BFX_OPERATOR_USER_ID` and `role == admin`;
- an account membership sufficient for the requested operation.

Account APIs use explicit paths:

```text
/api/v1/exchange-accounts/{exchange_account_id}/positions
/api/v1/exchange-accounts/{exchange_account_id}/offers
/api/v1/exchange-accounts/{exchange_account_id}/executions
/api/v1/exchange-accounts/{exchange_account_id}/credentials
/api/v1/exchange-accounts/{exchange_account_id}/config-draft
/api/v1/exchange-accounts/{exchange_account_id}/uncertainties
```

An authenticated principal without membership receives a non-enumerating not-found response. A non-operator is rejected before money-data routing while the product is operator-only.

The daemon is bootstrapped by mandatory `BFX_EXCHANGE_ACCOUNT_ID`. There is no `"default"` fallback. Missing, unknown, or inactive account identity makes live boot fail closed.

### 5.4 Operator-only containment

- A Better Auth server hook rejects the native `/sign-up/email` endpoint. Removing the UI alone is insufficient.
- `/register`, its Server Action, and registration navigation are removed.
- The production operator is selected only by immutable Better Auth user ID, never email.
- The operator must complete TOTP or passkey enrollment; a password-only session cannot mint an execution-console JWT.
- All non-operator users are disabled and their database/Redis sessions are revoked through an audited one-shot runbook.
- Direct signup, a valid general-user JWT, a forged account UUID, and a cross-account request all have explicit integration tests.

## 6. Ordered execution-state projection

### 6.1 Why row locking alone is insufficient

`SELECT ... FOR UPDATE` would prevent two transactions from losing a numeric update, but it would not establish a single order across:

- a submit acknowledgement;
- a WebSocket offer/fill message;
- a REST venue snapshot;
- a crash/restart recovery event.

It would also preserve the current aggregate-delta model, where a snapshot may already contain a change that a later-delivered stream message applies again. The target design therefore serializes events and projects venue objects by identity.

### 6.2 Event writer transaction

Every execution-state writer uses one `AccountEventWriter` boundary:

```text
begin transaction
  → pg_advisory_xact_lock(exchange_account_id, environment)
  → append event_log event(s)
  → run execution_state projector in event_seq order
  → update projection_heads
commit
```

- The lock covers an entire account/environment, not a symbol.
- Different accounts may project concurrently.
- No Bitfinex call occurs inside this database transaction.
- The daemon-lifetime writer lock proves one live worker owns venue-write authority; the transaction lock serializes concurrent tasks and protects against accidental cross-process projection writers.
- Event append, all affected projections, and the projection cursor either all commit or all roll back.

New schema-version-3 events have a mandatory stable `event_id UUID` with a partial unique constraint. Historical rows are not updated: their nullable stored `event_id` is derived as a deterministic UUIDv5 by the legacy upcaster from immutable stored-row fields. A schema-version check requires a stored event ID for every new version-3 row. The existing venue-field dedup remains a domain backstop, not the universal event identity.

An architecture test rejects direct execution-projection writes outside the event-store/projector package.

### 6.3 Projection cursor

`projection_heads` contains:

| Field | Contract |
|---|---|
| `exchange_account_id` | projection owner |
| `deployment_environment` | stream dimension |
| `projection_name` | `execution_state` |
| `last_event_seq` | last committed event cursor |
| `projector_version` | code/schema compatibility gate |
| `updated_at` | operational metadata only |

Because `event_seq` is globally increasing, the projector queries the next rows with `event_seq > last_event_seq`, filtered by account/environment and ordered ascending. Interleaved sequence numbers from another account are valid and are skipped.

Projection lag compares the cursor with the event-log head for the same account/environment, never the global head across all accounts.

### 6.4 Venue snapshot event

`set_position_snapshot()` is removed. A successful reconcile appends `VENUE_SNAPSHOT_OBSERVED` containing:

- query start and completion timestamps;
- symbol/wallet observations;
- complete normalized active-offer objects;
- complete normalized active-credit objects;
- source/request metadata needed to prove history coverage;
- no credentials or raw signed headers.

Offer and credit discovery is full-account, not limited to configured strategy symbols. Configured-symbol filtering is allowed only after every venue object has been persisted, counted, and classified; otherwise a manual or unexpected symbol could disappear from exposure authority.

The HTTP calls finish before the event-writer transaction begins. The snapshot is ordered wherever it commits in the local stream. A snapshot overwrites older object belief; a later event applies only if its venue identity/version is newer and transition-valid.

`reconcile_observation` becomes a rebuildable audit projection of this event rather than a second source of truth.

### 6.5 Entity-level venue projections

#### `venue_offer_state`

Keyed by `(exchange_account_id, environment, venue_offer_id)` and stores:

- symbol, type, original/current amount, rate, period, flags;
- venue creation/update timestamps and status;
- local attributed submission attempt/CID when known;
- last projected event sequence.

#### `venue_credit_state`

Keyed by `(exchange_account_id, environment, credit_id)` and stores:

- symbol, amount, rate, period, venue timestamps, and status;
- last projected event sequence.

The same object is upserted by submit acknowledgement, WebSocket event, and REST snapshot. Venue IDs are never reused. A terminal object cannot be reopened, and an older venue timestamp/sequence cannot overwrite newer state.

### 6.6 Position projection

The current delta names `reserved` and `realized` are replaced because active credits are lent principal, not realized profit. `position_state` is derived from entity projections and uncertainty state:

| Field | Meaning |
|---|---|
| `offered_amount` | active venue funding offers |
| `lent_amount` | active venue funding credits |
| `available_amount` | latest funding-wallet availability |
| `uncertain_amount` | full pessimistic reserve for unresolved ambiguous attempts |
| `last_venue_snapshot_at` | last authoritative reconcile observation |
| `last_event_seq` | projection cursor for this row |

Safety uses:

```text
gross_exposure = offered_amount + lent_amount + uncertain_amount
```

The projector updates `offer_claims.last_event_seq` correctly; the existing constant-zero behavior is removed. Offer lifecycle transitions are validated against an explicit transition table. An invalid transition rolls back the transaction and raises an active alert.

### 6.7 Deterministic rebuild

A clean rebuild reads only:

1. `event_log` rows;
2. versioned event decoders/upcasters;
3. deterministic projector code.

It never reads current time, a live venue API, environment-selected realm, or an old projection value. Live projections and a clean rebuild must be byte-for-byte equivalent after normalization of operational timestamps.

## 7. Ambiguous submit lifecycle

### 7.1 Separate submission result from venue lifecycle

A submission attempt, a venue offer, and exposure are separate concepts. The stringly typed `SubmittedOrder.status` is replaced with a closed result union:

- `SubmitAcknowledged`
- `SubmitRejected`
- `SubmitOutcomeUnknown`
- `SubmitNotSent`

Classification is deliberately asymmetric:

| Evidence | Outcome |
|---|---|
| Bitfinex `SUCCESS` plus offer ID | acknowledged |
| structured 2xx `ERROR`/`FAILURE` | rejected |
| explicit 4xx rejection | rejected |
| allowlisted, structurally parsed venue rejection | rejected |
| local validation failure before transport receives the request | not sent |
| timeout, connection reset, response loss | unknown |
| HTTP 5xx without explicit rejection proof | unknown |
| malformed success response | unknown |
| task cancellation after request start | unknown |
| restart finds intent without outcome | unknown |

UNKNOWN is the default. Arbitrary response text is not sufficient rejection evidence. The submit path and its HTTP transport have no automatic retry.

### 7.2 Submission attempt projection

`submission_attempts` is keyed by immutable `attempt_id UUID` and includes:

- unique `execution_decision_id`;
- exchange account/environment/symbol and internal CID;
- normalized exact request payload and fingerprint hash;
- request-start and outcome-observed timestamps;
- typed outcome and reason code;
- bound venue offer ID when known;
- last event sequence.

Each execution decision has at most one venue attempt. A restart resumes/resolves that attempt; it never creates a second attempt for the same decision.

### 7.3 Account command gate

The DB transaction lock does not cover the network gap. A live account worker therefore owns an `AccountCommandGate` around the whole money-write command:

```text
acquire account command gate
  → evaluate safety and uncertainty guards
  → persist exact submit intent
  → call Bitfinex
  → persist typed outcome
release gate
```

The gate may span a network call but never holds a DB transaction. It ensures the next submit cannot pass its guards before the preceding attempt's UNKNOWN block is durable. The worker lease/lifetime lock prevents another worker from bypassing the in-process gate.

### 7.4 Pessimistic reserve and scoped block

`SUBMIT_OUTCOME_UNKNOWN` produces an open uncertainty for `(exchange_account_id, environment, symbol)` and adds the full intended amount to `uncertain_amount`.

- The same attempt is never retried.
- New submits for the affected symbol are blocked.
- Other symbols continue when their own state is healthy.
- DB/read failure in the uncertainty guard blocks the submit.
- Exposure may be temporarily double-counted if the venue object is also visible; undercounting is forbidden.

### 7.5 Venue matching

Bitfinex active/history funding offers expose venue ID, symbol, creation time, original amount, rate, and period. The resolver queries active offers plus fully paginated history covering the attempt window.

The request fingerprint uses:

- symbol and offer type;
- original amount in venue-normalized precision;
- normalized rate;
- period and flags;
- a venue-clock-adjusted creation window;
- an unclaimed venue ID.

Resolution policy:

- exactly one exact candidate with complete query coverage: append `SUBMIT_MATCHED_TO_VENUE_OFFER` and bind it;
- zero candidates: remain UNKNOWN because absence is not proof of non-acceptance;
- multiple candidates: remain UNKNOWN; never select an arbitrary object;
- confirmed-not-accepted: operator-only resolution after a fresh successful reconcile and recorded evidence.

Relevant venue contracts:

- [Submit Funding Offer](https://docs.bitfinex.com/reference/rest-auth-submit-funding-offer)
- [Active Funding Offers](https://docs.bitfinex.com/reference/rest-auth-funding-offers)
- [Funding Offers History](https://docs.bitfinex.com/reference/rest-auth-funding-offers-hist)

## 8. Venue-object quarantine

### 8.1 Orphan offers

When a full account snapshot contains an active offer with no local attempt attribution, reconcile appends `VENUE_OFFER_QUARANTINED` instead of raising.

The projector:

- persists the full object in `venue_offer_state`;
- counts it in `offered_amount`;
- opens a block only for its account/symbol;
- preserves that no strategy decision/CID is known;
- continues processing other offers, credits, wallets, and symbols.

It never creates a synthetic CID, fabricates a reservation reference, assigns the offer to a strategy, or automatically cancels the offer.

Funding credits do not require originating-offer attribution because Bitfinex does not provide a reliable offer link on the credit object. They are always persisted by credit ID and counted in `lent_amount`. Unsupported symbols/exposure are recorded as a separate uncertainty class.

### 8.2 Rebuildable uncertainty state

`execution_uncertainties` is a projection with:

| Field | Meaning |
|---|---|
| account/environment/symbol | block scope |
| `kind` | `submit_outcome_unknown`, `unattributed_venue_offer`, or `unsupported_venue_exposure` |
| `correlation_key` | stable attempt or venue-object key |
| `status` | `open` or `resolved` |
| attempt/venue IDs | nullable typed links |
| `evidence` | bounded JSON evidence |
| open/resolve event seq | replay/audit linkage |
| resolution/operator ID | immutable operator audit |

Code never directly deletes or resolves a row. Resolution is another domain event.

### 8.3 Operator resolution

The operator API appends one of the supported resolution events:

- bind to a specified venue offer;
- confirm the request was not accepted;
- accept the object as an intentional manual/operator venue offer;
- confirm the unattributed object became terminal.

Every resolution records immutable operator user ID, a fresh reconcile observation ID, bounded evidence, reason, and timestamp. The API does not mutate the projection directly.

## 9. Runtime readiness and observability

Liveness, service readiness, and execution readiness are separate:

- `/health` means the web process is alive.
- `/ready` returns non-200 when the web API cannot use required DB/auth dependencies.
- account/symbol execution readiness is false when its projector, reconcile, or uncertainty invariants are unhealthy.

The worker remains alive while execution is blocked so reconcile, alerts, and operator recovery continue.

Required bounded-cardinality metrics and alerts:

- open execution uncertainties;
- projection lag and projector version mismatch;
- projector transaction failure;
- advisory-lock wait/failure;
- quarantined venue objects;
- venue-versus-projection exposure drift;
- stale venue snapshot;
- failed operator authorization;
- web-service readiness failure.

Account UUID, CID, venue object ID, and arbitrary error text stay in structured logs/audit, not Prometheus labels.

## 10. Migration and rollout

### 10.1 Release 0 — operator containment

This release does not stop the trading daemon:

1. reject signup server-side and remove registration surface;
2. deploy the immutable operator user-ID/role gate on every non-public web route;
3. enroll/require operator MFA;
4. revoke non-operator users and sessions;
5. add dependency-aware readiness;
6. prove direct signup and general-user JWT bypasses fail.

Membership lookup and account-scoped route migration intentionally wait for Halt 1, when their canonical tables and backfill exist.

### 10.2 Halt 1 — identity cutover

#### Preflight

1. Enumerate every distinct legacy account/environment realm.
2. Review and sign the explicit account mapping manifest.
3. Produce an encrypted offsite backup and restore it in isolation.
4. Record table counts, event-log head/hash, migration head, and venue snapshot.
5. Set persistent trading halt and prove `dry-evaluate` cannot submit.
6. Stop the worker after a final successful reconcile.

#### Migration

1. Apply additive schema revisions.
2. Seed exchange accounts and the sole operator membership from the manifest.
3. Run credential/config data migration and credential AAD re-encryption.
4. Backfill UUID ownership across all money tables.
5. Validate counts, nulls, uniqueness, and FK anti-joins.
6. Apply contract schema revisions and remove old realm columns/fallbacks.
7. Remove legacy scaffold only after zero-row assertions.
8. Run Alembic metadata drift checks.

There is no live dual-read. Additive, data, and contract steps occur while the worker is stopped.

#### Resume gates

- new code boots only with a valid active exchange-account UUID;
- event-chain continuity and identity-aware replay pass;
- a fresh venue snapshot reconciles without exposure drift;
- account API authorization and cross-account denial pass;
- the worker stays halted for one complete reconcile interval;
- `dry-evaluate` reports the expected guard result;
- an explicit operator action starts the bounded canary.

### 10.3 Halt 2 — execution-state cutover

#### Migration

1. Persist halt, stop worker, and create a second restore point.
2. Add event identity/cursor and new projection tables.
3. Install event upcasters and the deterministic projector.
4. Replay the complete event log into empty projections.
5. Compare the old aggregate view for diagnostics only; do not enable runtime dual-read.
6. Compare new entity projections with a fresh full-account Bitfinex snapshot.
7. Convert every old unresolved PENDING attempt into UNKNOWN.
8. Quarantine unattributed active offers without aborting reconcile.
9. Keep trading halted while any open uncertainty lacks an accepted resolution.

#### Canary

```text
halted boot
  → clean projection rebuild
  → fresh full-account reconcile
  → dry-evaluate
  → one minimum bounded submit
  → durable typed outcome
  → at least two reconcile cycles
  → DB/venue object and exposure comparison
  → restore the previous cap, never increase it
```

No strategy, config, cap, credential, or symbol expansion ships with this cutover.

### 10.4 Rollback policy

Before venue writes resume, the old image and pre-cutover DB restore point may be restored because the persistent halt proves no new venue command occurred.

After any possible venue write:

1. set/retain persistent halt;
2. preserve the event log and a fresh venue snapshot;
3. reconcile/adopt UNKNOWN and orphan objects;
4. forward-fix the code or projection;
5. restore a DB snapshot only if evidence proves there was no later venue mutation.

A blind DB restore after resuming venue writes is forbidden.

## 11. Testing and verification

### 11.1 Unit and model tests

- submit outcome classification truth table;
- all legal and illegal submission/offer/uncertainty transitions;
- zero/one/multiple fingerprint candidates;
- per-symbol uncertainty block scope and fail-closed DB errors;
- deterministic legacy event upcasting;
- projector purity and rebuild equivalence;
- terminal venue-object monotonicity.

### 11.2 PostgreSQL integration tests

SQLite cannot validate the critical behavior. CI uses real PostgreSQL for:

- transaction advisory locks;
- UUID FKs and partial uniqueness;
- 100 concurrent same-account appends without lost state;
- cross-account parallelism;
- event/projector/head atomic rollback;
- current-head-to-new-head Alembic migration;
- full event replay and clean projection rebuild.

### 11.3 Deterministic fault venue

A test-only Bitfinex transport records whether it accepted a request independently from what the client receives. It simulates:

- accept then connection drop;
- reject/no accept then connection drop;
- accept then malformed response;
- HTTP 5xx after side effect;
- crash after intent, during send, and before outcome persistence;
- REST snapshot and WebSocket delivery permutations;
- duplicate matching candidates.

Every scenario asserts:

- no duplicate venue attempt;
- exposure is never underestimated;
- only the correct account/symbol is blocked;
- reconcile continues for unaffected scope;
- clean rebuild reproduces resolution state.

### 11.4 Auth and frontend tests

- direct native signup is rejected;
- the registration route/action is absent;
- password-only or non-operator sessions cannot mint an operator JWT;
- cross-account access is denied without leaking account existence;
- the UI shows a blocking uncertainty banner and audited resolution flow;
- production frontend build and account-scoped API contract tests pass.

### 11.5 Required verification commands

Targeted tests run first. The final repository gate includes:

```bash
cd backend_py
uv run pytest -m "not integration"
uv run pytest -m integration
uv run mypy src/
uv run ruff check
uv run alembic check

cd ../frontend
pnpm test
pnpm lint
pnpm build
```

Migration verification also runs explicit row-count, FK anti-join, event-chain, projection-rebuild, and venue-comparison commands defined by the implementation plans.

## 12. Definition of done

- Native signup cannot be reached through UI, Server Action, or Better Auth endpoint.
- Production authorization uses immutable operator user ID plus account membership.
- Runtime account-scoped web queries do not read `BFX_ACCOUNT_ID` or use a default realm.
- Every money row belongs to an immutable exchange-account UUID.
- Credential and config-draft ownership is account-scoped.
- No execution projection is written outside the account event writer/projector.
- Live and clean-rebuild projections are equivalent.
- Ambiguous submissions become UNKNOWN and are never automatically retried.
- Unresolved UNKNOWN amount is pessimistically reserved and blocks only its account/symbol.
- Every venue offer/credit is persisted and counted; an orphan cannot abort unrelated reconcile work.
- Fault injection cannot produce an unrecorded duplicate submit.
- Both cutovers have executable, rehearsed halt/restore/reconcile runbooks.
- `backend_py/ARCHITECTURE.md`, repository deployment instructions, and the actual VM topology agree.

## 13. Deferred work and explicit gates

This specification does not implement:

- continuous offsite WAL/PITR or monthly restore automation beyond the mandatory pre-cutover restore point;
- desired/applied config versions, transactional outbox, worker lease/scheduler, or the final `CredentialProvider`;
- FORCE RLS/scoped database roles for external tenants;
- multi-version KMS/KEK rotation;
- static public proof artifacts and removal of dynamic public money-DB reads;
- generated OpenAPI client, immutable image promotion, or complete CI/CD hardening;
- external customer onboarding, billing, or real-money execution.

These are subsequent program workstreams, not optional leftovers. External real-money beta remains blocked until the parent ADR's safety, economic, DR, KMS/isolation, venue-consent, and legal gates all pass.

## 14. Implementation-plan decomposition

After this design is approved in written form, implementation is split into separate TDD plans:

1. **Operator-only containment and readiness**
2. **ExchangeAccount identity and Halt 1 cutover**
3. **Serialized execution projector and entity projections**
4. **UNKNOWN submit, quarantine, and operator resolution**
5. **Halt 2 migration, fault-injection verification, and bounded canary runbook**

Each plan must name exact files, migrations, tests, commands, commit boundaries, rollout gates, and rollback evidence. No plan may authorize remote deployment or live resume unless the operator explicitly starts that execution phase.
