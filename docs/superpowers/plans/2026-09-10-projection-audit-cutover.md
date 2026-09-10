# Projection Audit Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and rehearse lossless legacy projection archiving and atomic event-only cutover without authorizing production apply or trading resume.

**Architecture:** Archive the entire scoped pre-cutover projection set in a separate audit schema. Bind prepare, independent archive restore evidence, fresh venue snapshot and atomic apply to one immutable manifest. Extend isolated DR to verify both active event replay and archive contents.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, PostgreSQL 18, pytest, pgBackRest/R2.

**Spec:** `docs/superpowers/specs/2026-09-10-projection-audit-cutover-design.md` (approved 2026-09-10).

## Global Constraints

- 本設計不重新放寬 historical claim policy、live identity uniqueness 或事件 hash。
- 任一 unexplained difference 都停止，不加入 blanket allowlist。
- 一般 bot／webapi role 無 archive 寫入與刪除權限；已完成 run 不允許覆寫。
- 不新增自動 retention／清除政策；不刪除已封存歷史來縮短 restore。
- 沿既有 validated append 路徑寫入新 snapshot event；不得直接 SQL 插入跳過 provenance、identity 或 snapshot validation。
- 私有真實資料僅在 operator 隔離環境演練，不寫入 repository fixtures。
- 維持 Halt 2 **RPO <=300 秒、RTO <=60 秒**；<=3600 秒一般 DR 門檻不取代它。
- 不得把實作完成當作套用授權。任何一項未過，都維持 halt。
- Migration execution: `cd backend_py && uv run alembic upgrade head`; tests use pytest, not unittest.

## Dependencies and execution boundaries

Create an isolated worktree from the approved-docs main revision. Integrate reviewed commit
`2595041` into that branch before coding (preserve original commit via merge); do not push,
merge main, create a production `.env` symlink, switch tailnets or deploy during this plan.
Resolve integration conflicts before proceeding. Use synthetic local PostgreSQL fixtures.

Order: 1 → 2 → 3 → 4 → 5 → 6. Task 4 produces archive-only DR acceptance needed by Task 5;
full projection parity is deliberately not required to prove archive preservation before cutover.
Archive-only acceptance must never be accepted as a trading/complete DR receipt.
RTO instrumentation is a separate plan, `2026-09-10-dr-rto-measurement.md`; serialize any shared
DR-runner edits with Task 4. Current unknown claim/head field differences block production apply,
not implementation of diagnostic tooling. Neither plan promises a successful 60-second result.

## Shared file and data contracts

New package `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/` contains
`__init__.py`, `contracts.py`, `codec.py`, `diagnostics.py`, `tables.py`, `archive.py`,
`snapshot.py`, `apply.py`. No imports from operator scripts into runtime modules.

`contracts.py` defines frozen dataclasses (all IDs UUID, monetary values Decimal):

```python
@dataclass(frozen=True)
class Scope:
    account_id: UUID
    environment: str

@dataclass(frozen=True)
class StreamIdentity:
    count: int
    head: int
    digest: str

@dataclass(frozen=True)
class Difference:
    table: str
    key_digest: str
    column: str
    before_digest: str | None
    after_digest: str | None
    classification: str  # unexplained until evidence-backed classification

@dataclass(frozen=True)
class ArchiveManifest:
    run_id: UUID
    scope: Scope
    stream: StreamIdentity
    format_version: int
    image_digest: str
    migration_heads: tuple[str, ...]
    projector_version: str
    tables: tuple[dict[str, object], ...]  # exact table entries defined in Task 2
    digest: str
```

Reject unknown manifest fields, duplicate table entries, malformed digest, wrong version or scope;
absence is not equivalent to zero. Exactly these eight projection tables are allowed:
offer_claims, position_state, venue_offer_state, venue_credit_state, projection_heads,
reconcile_observation, submission_attempts, execution_uncertainties. Event rows are not duplicated
into archive; their original prefix is bound by StreamIdentity and independently verified.

### Task 1: Typed archive codec and field-level diagnostic report

**Files:** Create package init, `contracts.py`, `codec.py`, `diagnostics.py`;
`backend_py/tests/modules/execution/projection_cutover/test_codec.py`, `test_diagnostics.py`.
Modify `backend_py/scripts/verify_projection_replay.py`; add
`backend_py/tests/integration/test_projection_cutover_diagnostics.py`.

**Interfaces:** `encode_row(values: Mapping[str, object]) -> bytes`,
`decode_row(payload: bytes) -> dict[str, object]`,
`compare_rows(table: str, before: Sequence[Mapping[str, object]], after: Sequence[Mapping[str, object]], *, key_columns: tuple[str, ...]) -> tuple[Difference, ...]`.
Expose a narrowly scoped optional collector from temporary replay, default off, without changing
existing bounded CLI output/hash. Capture temporary rows before their connection closes; do not
query temporary tables using another connection.

- [ ] Write codec RED tests with UUID, Decimal('1.2300'), timezone-aware datetime, null and
  nested JSON; assert `decode_row(encode_row(row)) == row`, including preserved decimal exponent
  and timestamp representation. Reject float, NaN/infinity, unknown tags and naive timestamps.
  Use typed tags recursively so user JSON cannot collide with codec metadata.

  ```python
  def test_decimal_round_trip_preserves_scale():
      value = Decimal("1.2300")
      restored = decode_row(encode_row({"amount": value}))
      assert restored["amount"].as_tuple() == value.as_tuple()

  def test_rejects_float_money():
      with pytest.raises(ValueError):
          encode_row({"amount": 1.23})
  ```
- [ ] Add diagnostic RED tests: a changed surrogate ID or timestamp is reported for archive
  comparison, while existing replay canonical hashes keep their established exclusions.
  Unknown/different values default to `unexplained`; a label alone never authorizes apply.
- [ ] Run `uv run pytest tests/modules/execution/projection_cutover -q` in backend_py; retain RED.
- [ ] Implement stable UTF-8 JSON bytes with sorted object keys, explicit type tags and no float
  conversion. Derive row digests using SHA-256 of those bytes; never modify event hash code.
  Use stable primary-key encoding to pair rows, rejecting duplicate keys; expose only digests
  and column names in normal output. Private detailed output requires an explicit mode-0600 file.
- [ ] Add PG test against real temporary replay: capture source and rebuilt rows in one
  consistent source snapshot, report missing fUSD and changed claims/head fields without
  modifying source. Test another account/environment is never included.
- [ ] Run unit tests and `uv run pytest tests/integration/test_projection_cutover_diagnostics.py -m integration -q`;
  verify previous replay tests stay unchanged, then commit scoped files and request review.

### Task 2: Immutable archive schema, capture and full-content verification

**Files:** Create `tables.py`, `archive.py`, migration
`backend_py/alembic/versions/f8c2d4e6a901_add_projection_cutover_archive.py`;
modify `backend_py/alembic/env.py`;
modify `backend_py/src/bfx_funding_bot/core/alembic_compare.py` only if required for the named schema;
add `backend_py/tests/integration/test_projection_cutover_archive.py` and
`backend_py/tests/modules/execution/projection_cutover/test_manifest.py`.

**Interfaces:** `capture_archive(session: AsyncSession, *, scope: Scope, run_id: UUID, image_digest: str, projector_version: str) -> ArchiveManifest`;
`verify_archive(session: AsyncSession, *, expected: ArchiveManifest) -> None`.
Neither commits; caller owns transaction. Read metadata/event identity under the account writer
lock; capture schema descriptors and every row from the eight-table allowlist.

- [ ] Write PG RED tests for schema creation, exact row round-trip, completed-run immutability,
  duplicate run refusal, other-scope isolation, missing/extra row detection, mutation detection,
  ordinary runtime role denial and verifier SELECT-only. Migration must leave live rows unchanged.
- [ ] Run `uv run pytest tests/integration/test_projection_cutover_archive.py -m integration -q`.
- [ ] Implement schema `projection_audit`: `runs` (scope, identities, manifest, complete flag),
  `rows` (run_id/table/key, BYTEA encoded_payload, row_digest), `receipts` (run_id unique,
  snapshot identity, new StreamIdentity, active hashes, immutable apply evidence).
  Run and row writes occur in one transaction; completed runs/rows reject UPDATE/DELETE and new
  rows. Receipt is separately insert-only. Add foreign keys and uniqueness on (run_id, table, key).
  Keep run manifests immutable rather than updating them with apply status.
- [ ] Define each manifest table entry with exact fields `name`, `schema`, `key_columns`,
  `count`, `digest`. Sort rows by encoded key; hash length-delimited encoded rows so concatenation
  cannot collide. Root digest includes all metadata and ordered table entries except itself.
  Stream rows with bounded memory; verify schema and count independently, including empty tables.
- [ ] Migration parent must be verified current head (expected e7b1c2d3e4f5); if different,
  reconcile the migration graph, never silently create a second head. Enable named-schema
  metadata comparison without touching unrelated schemas. Refuse downgrade of populated archives.
- [ ] Use dedicated operator privileges, no PUBLIC writes. Test bot/webapi roles without
  superuser or archive ownership. Runtime privilege audit must FAIL for current superuser bot;
  production role correction requires a separately reviewed grants inventory/operator approval.
  Do not silently demote `bfx` or claim REVOKE constrains a superuser.
- [ ] Run migrations/`alembic check` only against disposable PG, then unit/PG tests and review.

### Task 3: Prepare CLI and complete-snapshot evidence boundary

**Files:** Create `snapshot.py`, `backend_py/scripts/cutover_projection.py`,
`backend_py/tests/scripts/test_cutover_projection.py`,
`backend_py/tests/modules/execution/projection_cutover/test_snapshot.py`.
Modify existing snapshot adapter only if tests prove the current normalized observations lack
required coverage: `backend_py/src/bfx_funding_bot/modules/execution/events.py` is not to be
changed just to invent zero balances or weaken coverage.

**Interfaces:** `validate_cutover_snapshot(snapshot: VenueSnapshotObserved, *, scope: Scope, managed_symbols: frozenset[str], now_ms: int, max_age_ms: int) -> None`.
CLI verbs `diagnose`, `prepare`, `verify-archive`, `apply`; apply remains rejected until Task 5.
`prepare` creates archive/manifest only, never appends snapshot or rebuilds projections.

- [ ] Write RED CLI tests: no default mutation, explicit scope/run/image required, safe outputs,
  refusal of symlink/existing evidence target, mode0600, no secret echo; apply is fail-closed.
  Snapshot tests reject missing managed symbol evidence, pagination gaps, future/stale/reversed
  query times, wrong UUID/env, partial dimensions and unknown exposure.
- [ ] Run `uv run pytest tests/scripts/test_cutover_projection.py tests/modules/execution/projection_cutover/test_snapshot.py -q`.
- [ ] Implement parsing with mutually exclusive verbs; invoke Task 1/2 functions through a
  session transaction. Existing normalized VenueSnapshotObserved and credential/vault loader
  are reused; accept no caller-provided boolean as proof of coverage or writer quiescence.
  Freeze source identity and require operator-reviewed, digest-bound classification evidence
  for every difference. Reject unexplained/new differences or classification artifact drift.
- [ ] Venue HTTP collection is read-only and outside the DB transaction. Resolve current
  snapshot/credential adapter callsites before wiring; test with recorded synthetic complete
  responses through the real parser. Never start daemon to acquire a snapshot. If the adapter
  cannot establish complete zero-balance symbol coverage, return a bounded blocking code.
- [ ] Add local PG prepare test: stream unchanged, archive verified, dry-run no persistent writes,
  completed run repeat verifies identical content or refuses changed inputs. Commit after GREEN/review.

### Task 4: Archive-aware DR evidence, including prepare-only restore mode

**Files:** Create `backend_py/scripts/verify_projection_archive.py`,
`backend_py/tests/scripts/test_verify_projection_archive.py`;
modify `deploy/vm/pgbackrest/{evidence.py,restore_drill.py,restore_commands.py}`,
`backend_py/tests/scripts/test_offsite_dr_{evidence,restore,bootstrap}.py`;
add `backend_py/tests/integration/test_projection_archive_dr.py`.

**Interfaces:** verifier consumes expected ArchiveManifest, returns bounded JSON with
`scope`, `run_id`, `manifest_digest`, `verified_tables`, `verified_counts`, `verified_digests`.
Use `verify_archive` to verify content, not merely stored digest columns. Prepared archive receipt
kind is `archive_restore`, complete trading DR remains kind `restore`. Preserve existing callers
for baseline without archives; upgraded schema with completed archives requires archive evidence.

- [ ] Write RED tests: tampered payload with unchanged stored hash, missing run/table/row,
  extra table, wrong scope/version, malformed/oversized evidence, stale receipt and missing SELECT.
  An archive_restore receipt must be rejected by every existing Halt 2/full DR acceptance path.
- [ ] Run `uv run pytest tests/scripts/test_verify_projection_archive.py tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_restore.py -q`.
- [ ] Extend baseline with independently captured archive manifests; never derive expected
  values from restored rows. Require exact completed-run inventory for scoped account. Feed
  verifier manifests through protected files, not secrets or unbounded environment values.
- [ ] Bootstrap ephemeral verifier with only archive schema USAGE and required SELECT.
  `--archive-only` still restores the selected backup, verifies original event identity/schema,
  disconnects egress and validates all requested archives; it does not claim old active parity.
  Normal restore mode also requires existing full active projection comparison. Add explicit
  bounded versioned receipt parsing, no truthy flags/default-empty archive bypass.
- [ ] Count archive verification inside existing restore deadline; cleanup failure invalidates
  either receipt kind. Integration tests use real pgBackRest role bootstrap and actual archive
  tampering on disposable PG. Commit after unit/PG GREEN and independent review.

### Task 5: Atomic apply, idempotency and failure injection

**Files:** Create `apply.py`,
`backend_py/tests/integration/test_projection_cutover_apply.py`;
modify `cutover_projection.py`, `contracts.py`, `test_cutover_projection.py`.

**Interfaces:** `apply_cutover(session: AsyncSession, *, expected: ArchiveManifest, snapshot: VenueSnapshotObserved, archive_restore_receipt: Mapping[str, object], now_ms: int) -> Mapping[str, object]`.
Caller owns transaction; session must already be transactional. Returned receipt contains run ID,
snapshot event ID, old/new stream identities, active projection hashes and archive digest.

- [ ] Write PG RED tests: valid apply, repeated completed run, wrong payload under same run ID,
  concurrent writer, source projection drift without new event, wrong image/head/hash/receipt,
  stale snapshot, unknown exposure, failures after each mutation, cross-account preservation.
  Assert complete raw row equality after rollback, not just count or a swallowed exception.
- [ ] Run `uv run pytest tests/integration/test_projection_cutover_apply.py -m integration -q`.
- [ ] Implement under existing SerializedAccountWriter account lock. Acquire lock before checking
  receipt/head/archive/live digest/halt. Verify completed-run idempotency before freshness checks:
  matching committed request returns existing receipt after current consistency verification,
  without appending the now-old snapshot. Different request fails.
- [ ] For a new run, require fresh matching archive_restore receipt, reviewed difference artifact
  and local operational quiescence checks; repeat source checks under lock. Refuse superuser
  runtime identity, missing role-denial evidence and unverifiable writer status.
- [ ] Use existing `PostgresEventStore.append_snapshot` validated path, then full scoped rebuild.
  Existing account locks must be transaction-compatible; no nested commit or parallel writer
  session. Verify original event prefix unchanged and new head bound to actual appended snapshot.
- [ ] Invoke existing temporary replay using the transaction's captured event rows, not a fresh
  source connection that cannot see uncommitted snapshot. Compare live projections in the same
  transaction with the independent temporary rebuild. Verify every exposure dimension/coverage,
  archive again, then insert receipt; caller commits once. No HTTP operation inside locks.
- [ ] Inject failures before/after append, rebuild, parity, receipt and commit. Restart the
  CLI after ambiguous commit and prove receipt-based exactly-once behavior. Post-commit failure
  must retain halt and events, never restore archived rows automatically. Commit/review after GREEN.

### Task 6: Release verification, runbook and isolated rehearsal handoff

**Files:** Create `docs/runbooks/projection-audit-cutover.md`;
modify `docs/runbooks/offsite-dr.md`, `backend_py/ARCHITECTURE.md`;
extend `backend_py/tests/integration/test_projection_cutover_apply.py` with end-to-end flow.

**Interfaces:** consumes reviewed Tasks 1–5; produces release/test evidence and an operator
approval package, not an automatic production rollout.

- [ ] Add a PG end-to-end test: diagnose → archive → independently verify restored archive →
  fresh snapshot → atomic apply → repeat apply → independent new baseline → archive+active verify.
  Include fUSD-zero with actual complete coverage, legacy credit audit-only events, two synthetic
  historical CID cycles, old checkpoints, unexpected symbol and second account.
- [ ] Run `uv run pytest tests/modules/execution/projection_cutover tests/scripts/test_cutover_projection.py -q`;
  run all new PG integration tests and existing Halt 2/serialized writer/DR integration tests.
  Run `uv run pytest -m 'not integration' -q`, `uv run ruff check`, `uv run mypy src/` and
  `git diff --check`. Preserve exact outputs/warnings; fix regressions before commit/review.
- [ ] Runbook documents diagnose/prepare/archive-only restore/apply/complete restore commands,
  role-denial prerequisites, controlled writers, safe before-commit rollback, after-commit halt
  and forward repair. Never provide automatic old-DB restoration after venue writes.
- [ ] Perform final independent whole-branch review. On restored private data, classify actual
  claim/head differences, rehearse archive preservation and apply without venue writes; destroy
  only generated isolated resources and independently verify absence. Do not fabricate fresh
  production snapshot evidence from historical fixture or old private capture.
- [ ] Stop for operator approval before production schema/grants/archive/apply. Provide exact
  account/environment, manifest/image, backup label, head/hash, classified diff and rollback evidence.
  Current company tailnet is not permission to switch machine-global profile automatically.
- [ ] Runtime UUID/KEK/owner identity and legacy-secret removal, auth denial, no uncertainty,
  fresh exposure, RPO/RTO and bounded canary/two fresh reconciles remain separate release gates.
  No main merge/push/trading resume is implied by successful implementation.

## Self-review coverage

Spec 1 → Tasks 1–2; Spec 2 → Tasks 1,3,4,6; Spec 3 → Task 5; Spec 4 → Tasks 5–6;
Spec 5 → Task 4 plus separate RTO measurement plan; Spec 6 → every task and Task 6 release gate.
Archive-only receipt breaks the prepare/old-parity dependency without weakening full DR.
Privileges must be proven for actual runtime roles; superuser is an explicit unresolved operator gate.
