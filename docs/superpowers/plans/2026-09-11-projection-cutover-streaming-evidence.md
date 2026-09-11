# Projection Cutover Streaming Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unbounded v1 diagnostic/classification payloads with local, digest-pinned v2 evidence directories that preserve every difference while keeping Python memory bounded through generation, review, prepare, and apply.

**Architecture:** A runtime-owned evidence module writes a small codec manifest and length-delimited chunk files. The replay kernel streams one projection table at a time through a bounded external spool and writes one `Difference` record at a time. A complete pre-lock verifier returns a compact typed summary; `apply_cutover` rechecks that summary against the live transaction without reading hundreds of MiB while holding the account lock.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, PostgreSQL 18.6, existing versioned `encode_row` codec, SQLite temporary spool, `pytest`/`pytest-asyncio`, `ruff`, `mypy`.

**Spec:** `docs/superpowers/specs/2026-09-11-projection-cutover-streaming-evidence-design.md`

## Global Constraints

- Preserve every existing per-column `Difference`; do not collapse missing rows or omit columns.
- Diagnostic and classification artifacts are local private directories; do not introduce R2, compression, or a new runtime dependency.
- v2 artifact limits are 1 MiB per encoded record, 4 MiB per part, 2 GiB per artifact, and 4,096 parts.
- Directory mode is `0700`; manifest/part files are `0600`; reject symlinks, wrong owner/mode, extra entries, incomplete markers, and existing targets.
- Part frames are `uint64_be(payload_length) + encode_row(record)`; all declared and observed counts/digests must match.
- Existing v1 single-file reading remains for legacy inspection; new diagnose/prepare/apply flow accepts v2 evidence only.
- `apply_cutover` must receive typed verified evidence, never a caller boolean or raw evidence list.
- Full artifact verification occurs before the account lock; in-lock checks rebind compact identity/digests to the live database and receipt.
- Caller owns the apply transaction; no nested commit, HTTP operation, production grant, tailnet, deployment, or trading-resumption operation is part of this plan.
- Every implementation change has pytest coverage; run tests from `backend_py/` with `uv run`.
- Use the repository's Conventional Commit hook and preserve unrelated working-tree changes.

---

## Task 1: Close the already-approved Task 5 freshness checkpoint

**Files:**

- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py` in `_projection_evidence`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py`
- Test: `backend_py/tests/modules/execution/projection_cutover/test_snapshot.py`
- Test: the existing focused tests in `backend_py/tests/integration/test_projection_cutover_apply.py` for loaded projection rows
- Preserve without staging: the unfinished `backend_py/tests/integration/test_projection_cutover_apply.py` apply cases until Task 5

**Interfaces:**

- Consumes: the reviewed Task 4b replay kernel at `30bac5d`.
- Produces: three `populate_existing=True` projection/exposure reads in `_projection_evidence`, independent finite validation of `VenueOfferObservation.amount_original`, and a clean reviewed base for the new evidence tasks.

The working tree already contains the RED/GREEN work for this checkpoint. Finish only those approved changes before introducing the v2 protocol. Do not create `apply.py`, change canonical hashes, call operator scripts from runtime code, or stage the unfinished integration apply file.

- [ ] **Step 1: Re-run the focused RED/GREEN evidence for the existing checkpoint.**

Run from `backend_py/`:

```bash
uv run pytest tests/modules/execution/projection_cutover/test_snapshot.py -q
uv run pytest \
  tests/integration/test_projection_cutover_apply.py::test_parity_evidence_detects_database_drift_with_loaded_identity_map \
  tests/integration/test_projection_cutover_apply.py::test_exposure_evidence_refreshes_loaded_rows \
  -m integration -q
```

Expected: the finite snapshot tests and both loaded-row freshness tests pass; no apply module is imported by this checkpoint.

- [ ] **Step 2: Inspect the diff and confirm the narrow boundary.**

Run:

```bash
git diff -- \
  backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py \
  backend_py/tests/modules/execution/projection_cutover/test_snapshot.py
```

The diff must contain only the three fresh ORM reads, finite `amount_original` validation, and their tests. If another behavior is present, remove it before proceeding.

- [ ] **Step 3: Run static checks for the checkpoint.**

```bash
cd backend_py
uv run ruff check \
  src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py \
  tests/modules/execution/projection_cutover/test_snapshot.py
uv run mypy \
  src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py
```

Expected: both commands pass without warning suppression.

- [ ] **Step 4: Commit only the completed checkpoint.**

```bash
git add \
  backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py \
  backend_py/tests/modules/execution/projection_cutover/test_snapshot.py
git commit -m "fix(execution): harden cutover projection evidence"
```

Do not add the untracked apply integration file. Review this commit independently before Task 2.

---

## Task 2: Implement the v2 evidence directory protocol

**Files:**

- Create: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/evidence.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/contracts.py` only if shared identity types are needed
- Create: `backend_py/tests/modules/execution/projection_cutover/test_evidence.py`

**Interfaces:**

- Consumes: `Scope`, `StreamIdentity`, and `encode_row`/`decode_row` from the reviewed projection-cutover modules.
- Produces the exact runtime types used by Tasks 3–5:

```python
@dataclass(frozen=True, slots=True)
class ChunkDescriptor:
    name: str
    record_count: int
    byte_count: int
    digest: str

@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    kind: str
    format_version: int
    run_id: UUID
    scope: Scope
    image_digest: str
    projector_version: str
    stream: StreamIdentity
    record_kind: str
    record_count: int
    total_bytes: int
    chunks: tuple[ChunkDescriptor, ...]
    root_digest: str
    digest: str
    tables: tuple[Mapping[str, object], ...] = ()
    diagnostic_digest: str | None = None
    reviewer: str | None = None

@dataclass(frozen=True, slots=True)
class VerifiedCutoverEvidence:
    diagnostic: EvidenceManifest
    classification: EvidenceManifest
    classification_counts: tuple[tuple[str, int], ...]

class EvidenceWriter:
    @classmethod
    def create(cls, path: Path, *, manifest_fields: Mapping[str, object]) -> "EvidenceWriter":
        raise NotImplementedError
    def append(self, record: Mapping[str, object]) -> None:
        raise NotImplementedError
    def finish(self) -> EvidenceManifest:
        raise NotImplementedError

def iter_verified_records(
    path: Path, *, expected_digest: str, expected_kind: str,
) -> Iterator[dict[str, object]]:
    raise NotImplementedError

def verify_artifact(
    path: Path, *, expected_digest: str, expected_kind: str,
) -> EvidenceManifest:
    raise NotImplementedError
```

`EvidenceWriter.create` validates the allowed v2 kind and record kind before writing. `finish()` writes the manifest and `COMPLETE` marker only after all frame and root digests are finalized. `iter_verified_records()` is a fully consuming iterator contract: callers must exhaust it, and `verify_artifact()` does so internally.

- [ ] **Step 1: Write failing tests for typed manifest and frame invariants.**

Add pytest fixtures that construct a valid diagnostic header and records:

```python
def test_writer_round_trip_and_digest(tmp_path):
    writer = EvidenceWriter.create(tmp_path / "diagnostic", manifest_fields=header)
    writer.append({"table": "position_state", "key_digest": "a" * 64,
                   "column": "last_updated_ms", "before_digest": "b" * 64,
                   "after_digest": "c" * 64, "classification": "unexplained"})
    manifest = writer.finish()
    assert manifest.record_count == 1
    assert list(iter_verified_records(
        tmp_path / "diagnostic", expected_digest=manifest.digest,
        expected_kind="projection-cutover-diagnostic-v2",
    ))[0]["table"] == "position_state"
```

Cover empty artifacts, multiple chunks, deterministic manifest/root/part digests, exact byte counts, and rejection of unsupported kinds or record kinds.

- [ ] **Step 2: Run the new tests and verify they fail for the missing protocol.**

```bash
cd backend_py && uv run pytest tests/modules/execution/projection_cutover/test_evidence.py -q
```

Expected: collection or assertion failure because `evidence.py` and its writer/reader do not yet exist.

- [ ] **Step 3: Implement bounded frame writing and digest calculation.**

Use an 8-byte big-endian payload length, reject payloads over `1 << 20`, rotate before a part exceeds `4 << 20`, and retain only the current part buffer plus at most 4,096 small descriptors. Hash exact frame bytes for each part and for the ordered root stream. Use the existing `encode_row`; never serialize raw projection rows.

- [ ] **Step 4: Implement exclusive private directory creation and finalization.**

Create the requested target with `path.mkdir(mode=0o700, exist_ok=False)`, create `chunks` with `0700`, write parts and manifest files with `0600`, fsync each completed file and its parent directory, then create `COMPLETE` last. Refuse an existing path and leave an interrupted directory unreadable because it lacks `COMPLETE` or a complete manifest. Do not overwrite targets.

- [ ] **Step 5: Implement the fail-closed reader.**

Open the directory and every file with no-follow checks. Require current owner, exact modes, only `manifest`, `COMPLETE`, and `chunks` entries, listed part names, lexical order, valid frames, EOF at the declared boundary, digest/count equality, `total_bytes <= 2 GiB`, and at most 4,096 parts. Reject malformed codec data before yielding a record.

- [ ] **Step 6: Run protocol tests and static checks.**

```bash
cd backend_py
uv run pytest tests/modules/execution/projection_cutover/test_evidence.py -q
uv run ruff check \
  src/bfx_funding_bot/modules/execution/projection_cutover/evidence.py \
  tests/modules/execution/projection_cutover/test_evidence.py
uv run mypy src/bfx_funding_bot/modules/execution/projection_cutover/evidence.py
```

Expected: all round-trip, tamper, limit, permission, symlink, incomplete-directory, and static checks pass.

- [ ] **Step 7: Commit the protocol.**

```bash
git add \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/evidence.py \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/contracts.py \
  backend_py/tests/modules/execution/projection_cutover/test_evidence.py
git commit -m "feat(execution): add bounded cutover evidence artifacts"
```

Review this task before starting the streaming producer.

---

## Task 3: Add bounded streaming projection comparison

**Files:**

- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/diagnostics.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py`
- Modify: `backend_py/scripts/verify_projection_replay.py` only for compatibility re-exports/call wiring
- Modify: `backend_py/tests/modules/execution/projection_cutover/test_diagnostics.py`
- Modify: `backend_py/tests/integration/test_captured_projection_replay.py`
- Create or extend: `backend_py/tests/integration/test_projection_cutover_diagnostics.py`

**Interfaces:**

- Consumes: `EvidenceWriter`/`EvidenceManifest` from Task 2 and the existing temporary replay kernel.
- Produces this typed sink boundary without importing operator scripts into runtime code:

```python
class DifferenceSink(Protocol):
    def begin_table(self, name: str, key_columns: tuple[str, ...]) -> None:
        pass
    def append(self, difference: Difference) -> None:
        pass
    def finish_table(self, table_facts: Mapping[str, object]) -> None:
        pass

async def replay_captured_rows(
    session: AsyncSession,
    *,
    rows: Sequence[EventLogRow],
    account_id: UUID,
    environment: str,
    projector_version: str = DEFAULT_PROJECTOR_VERSION,
    expected_event_hash: str | None = None,
    difference_sink: DifferenceSink | None = None,
) -> ReplayReport:
    raise NotImplementedError
```

The existing report, event hash, default replay behavior, and public script output remain unchanged when `difference_sink` is `None`. With a sink, each table is read through an async cursor into a private SQLite spool keyed by encoded primary-key bytes; two sorted spool cursors are merged and emit differences one at a time. No `list()` of all physical source or rebuilt rows is allowed in the diagnostic path.

- [ ] **Step 1: Add RED tests proving one-shot streams and full missing-row coverage.**

Test the pure comparator with iterators that reject `len()` and indexing, and verify a missing twelve-column row emits twelve differences in deterministic key/column order. Add a synthetic 148,768-row case that writes records to v2 chunks and asserts the manifest count and per-column coverage without materializing a `differences` list.

```python
def test_streaming_missing_row_keeps_every_physical_column():
    before = one_shot_rows([{
        "id": 1, "account_id": ACCOUNT, "exchange_account_id": ACCOUNT,
        "deployment_environment": "ci", "symbol": "fUSD",
        "reserved_usdt": Decimal("1"), "realized_usdt": Decimal("0"),
        "n_offers": 0, "n_credits": 0, "observed_at_ms": 1,
        "event_seq_fence": 1, "recorded_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }])
    after = one_shot_rows([])
    changes = tuple(compare_sorted_rows(before, after, key_columns=KEYS))
    assert len(changes) == 12
    assert [change.column for change in changes] == sorted(PHYSICAL_COLUMNS)
```

Keep existing small `compare_rows` tests to preserve its compatibility contract.

- [ ] **Step 2: Run the new RED tests.**

```bash
cd backend_py && uv run pytest \
  tests/modules/execution/projection_cutover/test_diagnostics.py \
  tests/integration/test_projection_cutover_diagnostics.py -q
```

Expected: the streaming comparator/sink path is missing or still attempts sequence materialization.

- [ ] **Step 3: Implement row streaming and bounded external spooling.**

Add an async projection-row iterator that selects the exact physical columns, casts JSON columns to `Text`, decodes JSON numbers with `Decimal`, preserves top-level `JSON_NULL`, and yields mappings from a server-side cursor. Insert encoded key/payload bytes into a private `0700`/`0600` SQLite spool, reject duplicate keys, and merge source/rebuilt cursors without retaining all rows in Python.

- [ ] **Step 4: Wire the sink into the shared replay kernel.**

Replace the diagnostic-only `_archive_projection_rows` list path with per-table cursor/spool processing. Keep source and replay rows in the same MVCC evidence boundary, preserve `populate_existing=True` projection reads, and leave the no-sink replay path unchanged. Do not change event deserialization, upcasting order, projector logic, or canonical event hashes.

- [ ] **Step 5: Run focused tests and static checks.**

```bash
cd backend_py
uv run pytest \
  tests/modules/execution/projection_cutover/test_diagnostics.py \
  tests/integration/test_captured_projection_replay.py \
  tests/integration/test_projection_cutover_diagnostics.py -q
uv run ruff check \
  src/bfx_funding_bot/modules/execution/projection_cutover/diagnostics.py \
  src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  tests/modules/execution/projection_cutover/test_diagnostics.py
uv run mypy \
  src/bfx_funding_bot/modules/execution/projection_cutover/diagnostics.py \
  src/bfx_funding_bot/modules/execution/event_store/replay_verification.py
```

Expected: all old replay regressions and new 148,768-row bounded coverage checks pass.

- [ ] **Step 6: Commit the streaming producer.**

```bash
git add \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/diagnostics.py \
  backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  backend_py/scripts/verify_projection_replay.py \
  backend_py/tests/modules/execution/projection_cutover/test_diagnostics.py \
  backend_py/tests/integration/test_captured_projection_replay.py \
  backend_py/tests/integration/test_projection_cutover_diagnostics.py
git commit -m "feat(execution): stream projection cutover diagnostics"
```

Review the diff and verify no runtime module imports `scripts.*` before Task 4.

---

## Task 4: Integrate v2 diagnose, classification, and prepare

**Files:**

- Modify: `backend_py/scripts/cutover_projection.py`
- Modify: `backend_py/tests/scripts/test_cutover_projection.py`
- Modify: `backend_py/tests/integration/test_projection_archive_dr.py` only where fixtures must identify v2 evidence
- Modify: `backend_py/tests/integration/test_projection_cutover_diagnostics.py`

**Interfaces:**

- Consumes: `DifferenceSink`, `EvidenceManifest`, and `VerifiedCutoverEvidence` from Tasks 2–3.
- Produces:

```python
def verify_cutover_evidence(
    diagnostic_path: Path,
    classification_path: Path,
    *,
    expected_diagnostic_digest: str,
    expected_classification_digest: str,
    expected_run_id: UUID,
    expected_scope: Scope,
    expected_image_digest: str,
    expected_projector_version: str,
) -> VerifiedCutoverEvidence:
    raise NotImplementedError

async def diagnose(
    factory: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    run_id: UUID,
    image_digest: str,
    projector_version: str,
    output: Path,
) -> EvidenceManifest:
    raise NotImplementedError
```

`verify_cutover_evidence` fully consumes both directories before returning. It checks diagnostic/classification identity, manifest digests, matching record count, exact one-to-one order, all current binding fields, nonempty reviewer/reason/evidence, and only the existing allowed classes (`identity_representation`, `historical_state`, `time_sequence`, `missing_symbol`, `checkpoint_source`). It returns only the compact summary; no record list escapes the verifier.

- [ ] **Step 1: Add RED CLI tests for directory-based diagnose and lockstep classification.**

Cover output directory creation, v2 manifest/result digest, existing target refusal, malformed/incomplete directory rejection, diagnostic/classification identity mismatch, duplicate/missing/reordered records, `unexplained` and unknown classes, empty reason/evidence, and exact summary counts.

```python
def test_prepare_requires_v2_evidence_directories(tmp_path):
    args = parse_args(valid_prepare_args(
        diagnostic=tmp_path / "diagnostic.json",
        classification=tmp_path / "classification.json",
    ))
    with pytest.raises(ValueError, match="evidence_format_invalid"):
        asyncio.run(run_command(args))
```

Keep v1 reader tests for legacy inspection; the new prepare path must reject v1 rather than reinterpret it.

- [ ] **Step 2: Run the RED CLI tests.**

```bash
cd backend_py && uv run pytest \
  tests/scripts/test_cutover_projection.py \
  tests/integration/test_projection_cutover_diagnostics.py -q
```

Expected: the current single-payload diagnose/classification path fails the new directory and v2 assertions.

- [ ] **Step 3: Wire `diagnose` to the streaming sink.**

Create the diagnostic directory before replay starts, instantiate an `EvidenceWriter` with `projection-cutover-diagnostic-v2`, and append each sink difference directly. Finish with the existing stream identity, exact table facts, and manifest digest. Return only the manifest and a bounded CLI JSON result containing status, digest, and count.

- [ ] **Step 4: Implement the lockstep classification verifier.**

Use `iter_verified_records` for both directories. Iterate the diagnostic and classification streams together, reject early EOF or extra records, compare classification binding fields to the diagnostic record with its classification reset to `unexplained`, and count allowed classes. Verify the classification manifest's `diagnostic_digest` equals the diagnostic manifest digest before reading records.

- [ ] **Step 5: Change `prepare_archive` to accept compact v2 evidence.**

Replace the current `diagnostic: dict` and `classification_payload: bytes` inputs with `VerifiedCutoverEvidence`. Compare diagnostic manifest table facts to the archive manifest, validate snapshot/operations as already specified, and write the existing small `projection-cutover-prepared-v1` envelope with only digest/identity references. `prepare` must not copy chunk bytes into the envelope and must not hold the account lock during artifact reads.

- [ ] **Step 6: Run integration, static, and regression checks.**

```bash
cd backend_py
uv run pytest \
  tests/scripts/test_cutover_projection.py \
  tests/integration/test_projection_cutover_diagnostics.py \
  tests/integration/test_projection_archive_dr.py -q
uv run ruff check scripts/cutover_projection.py tests/scripts/test_cutover_projection.py
uv run mypy scripts/cutover_projection.py
```

Expected: v2 directory flow passes, v1 prepare/apply is rejected, archive fixtures still validate their protected prepared envelope, and no raw evidence is emitted in CLI output.

- [ ] **Step 7: Commit CLI and prepare integration.**

```bash
git add \
  backend_py/scripts/cutover_projection.py \
  backend_py/tests/scripts/test_cutover_projection.py \
  backend_py/tests/integration/test_projection_cutover_diagnostics.py \
  backend_py/tests/integration/test_projection_archive_dr.py
git commit -m "feat(execution): bind prepare to streaming cutover evidence"
```

Review artifact identity and classification coverage independently before resuming apply.

---

## Task 5: Resume atomic apply with compact verified evidence

**Files:**

- Create: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/apply.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/contracts.py` if a typed quiescence input is required
- Modify: `backend_py/scripts/cutover_projection.py`
- Modify: `backend_py/tests/scripts/test_cutover_projection.py`
- Modify: `backend_py/tests/integration/test_projection_cutover_apply.py`
- Preserve and include the already-created finite snapshot and fresh-DB evidence changes from Task 1

**Interfaces:**

- Consumes: `ArchiveManifest`, `VenueSnapshotObserved`, `VerifiedCutoverEvidence`, the reviewed v2 archive-restore receipt, and a typed injected quiescence verifier.
- Produces:

```python
class QuiescenceVerifier(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        scope: Scope,
        runtime_roles: tuple[str, ...],
        operation_digest: str,
    ) -> None:
        pass

async def apply_cutover(
    session: AsyncSession,
    *,
    expected: ArchiveManifest,
    snapshot: VenueSnapshotObserved,
    archive_restore_receipt: Mapping[str, object],
    evidence: VerifiedCutoverEvidence,
    runtime_roles: tuple[str, ...],
    operation_digest: str,
    quiescence_verifier: QuiescenceVerifier,
    now_ms: int,
) -> Mapping[str, object]:
    raise NotImplementedError
```

The callback is a typed injected verifier, not a boolean. `apply.py` must not import `scripts.projection_cutover_operations` or any other operator module. The CLI performs full artifact verification before entering the transaction and supplies the compact summary; `apply_cutover` rechecks all identity/digest fields, receipt schema/target, archive manifest, runtime role denial, current head/live hashes, snapshot freshness, and exposure coverage after acquiring `AccountEventWriter`'s lock.

- [ ] **Step 1: Replace the unfinished apply test payloads with v2 fixture helpers.**

Use `EvidenceWriter` to create a diagnostic and classification directory, call `verify_cutover_evidence`, and pass `VerifiedCutoverEvidence` to the runtime. Do not put `diagnostic_payload`, `classification_payload`, or any full difference list into the typed runtime input. Keep prepared/receipt/archive transport bytes bounded and digest-pinned.

- [ ] **Step 2: Write RED tests for the full precondition matrix.**

Add tests for valid apply, matching completed-run idempotency before freshness, different request under the same run, wrong v2 archive target/receipt digest, wrong evidence digest/run/scope/image/projector, stale snapshot, unknown exposure, source projection drift, missing role denial, concurrent writer, and operation/quiescence failure.

- [ ] **Step 3: Run the RED integration tests.**

```bash
cd backend_py && uv run pytest tests/integration/test_projection_cutover_apply.py -m integration -q
```

Expected: collection fails because `apply.py` or the final v2 evidence interface is not implemented, while the previously completed replay freshness tests remain green.

- [ ] **Step 4: Implement the transaction gate and idempotency order.**

Require an active caller transaction. Acquire the existing account lock first, then verify completed-run identity before stale freshness checks. A matching committed request performs current consistency verification and returns the immutable receipt without new HTTP, snapshot append, or rebuild; a different request fails.

- [ ] **Step 5: Implement the mutation and parity sequence.**

Run the injected quiescence check and all identity/halt/archive checks under the lock. Append through `PostgresEventStore.append_snapshot`, rebuild the full scoped projection, verify original event prefix and actual new head, replay caller-captured event rows through the reviewed shared kernel, compare actual database rows with fresh ORM reads, verify every exposure dimension, capture/verify the new archive, and insert the immutable receipt. Do not commit inside the function.

- [ ] **Step 6: Implement failure injection at every boundary.**

Inject failures before/after append, rebuild, parity, and receipt insertion; assert complete raw-table equality after rollback, including events, halt, projection-audit rows, and receipts. Add an ambiguous-commit restart test proving receipt-based exactly-once behavior. Add a post-commit failure test proving the new event/projection and halt remain and archived rows are never restored automatically.

- [ ] **Step 7: Wire the CLI apply path without large in-lock I/O.**

Read and verify protected prepared/receipt/operations files and both v2 evidence directories before opening the apply transaction. Pass only bounded typed summaries and receipt mappings into `apply_cutover`; reject v1 evidence and the old `apply_not_implemented` path. No venue HTTP call may occur while holding the account lock.

- [ ] **Step 8: Run complete Task 5 verification.**

```bash
cd backend_py
uv run pytest tests/integration/test_projection_cutover_apply.py -m integration -q
uv run pytest \
  tests/modules/execution/projection_cutover \
  tests/integration/test_captured_projection_replay.py \
  tests/integration/test_projection_cutover_diagnostics.py \
  tests/scripts/test_cutover_projection.py -q
uv run ruff check \
  src/bfx_funding_bot/modules/execution/projection_cutover \
  scripts/cutover_projection.py tests/integration/test_projection_cutover_apply.py
uv run mypy src/ scripts/cutover_projection.py
git diff --check
```

Expected: all focused/unit/PG tests and static checks pass, with the existing four unrelated warnings preserved and reported rather than suppressed.

- [ ] **Step 9: Commit the completed atomic apply.**

```bash
git add \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/apply.py \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/contracts.py \
  backend_py/scripts/cutover_projection.py \
  backend_py/tests/scripts/test_cutover_projection.py \
  backend_py/tests/integration/test_projection_cutover_apply.py \
  backend_py/src/bfx_funding_bot/modules/execution/event_store/replay_verification.py \
  backend_py/src/bfx_funding_bot/modules/execution/projection_cutover/snapshot.py \
  backend_py/tests/modules/execution/projection_cutover/test_snapshot.py
git commit -m "feat(execution): apply projection cutover atomically"
```

Run an independent two-stage review before treating Task 5 as complete.

---

## Handoff to the existing release task

After Tasks 1–5 are reviewed clean, continue Task 6 in
`docs/superpowers/plans/2026-09-10-projection-audit-cutover.md`:

- extend the end-to-end diagnose → v2 classify → prepare → archive-only verify → apply → repeat flow;
- document v2 evidence-directory handling and bounded cleanup in `docs/runbooks/projection-audit-cutover.md`;
- run the full non-integration suite, all new PostgreSQL integration tests, `ruff`, `mypy`, and `git diff --check`;
- perform final independent review and operator approval gates;
- keep production role/credential/R2/Tailscale changes and trading resumption outside this implementation plan.

The RTO measurement plan remains a separate workstream and must not be claimed as complete by this evidence implementation.
