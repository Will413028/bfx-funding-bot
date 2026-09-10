# Projection Cutover Streaming Evidence Design

**Date:** 2026-09-11
**Status:** Approved in design discussion; implementation not started
**Parent design:** `2026-09-10-projection-audit-cutover-design.md`

## Context

The projection cutover implementation currently represents diagnostic differences
as one codec-encoded mapping containing a complete `differences` list. That shape
does not scale to the real captured source. The historical source has 148,768
`reconcile_observation` rows while the replayed projection is empty. The current
comparison emits one difference for every physical column of each missing row.
With the reviewed codec this is at least 578,112,502 bytes (551.33 MiB) before
classification reasons/evidence, while `read_private` has a 64 MiB limit.

This capacity result was computed from schema metadata and synthetic encoded
differences; no private production rows were read. Raising the limit alone would
still leave the current list materialization and unbounded decoded object tree.

## Goals

- Keep every existing per-column `Difference` and classification coverage.
- Bound process memory while producing, validating, and consuming evidence.
- Bind diagnostic and classification evidence to immutable identity and digests.
- Keep evidence local and private; do not introduce R2, compression, or a new
  runtime dependency for this change.
- Keep the account-lock transaction free of large filesystem reads.
- Preserve the existing v1 reader for legacy inspection, while making new
  prepare/apply cutovers v2-only.

## Non-goals

- Do not collapse missing rows into one sentinel or omit columns.
- Do not make classifications authoritative without a complete reviewed record.
- Do not change canonical event hashes, projector semantics, archive schema, or
  PostgreSQL source data.
- Do not infer blanket classifications from table names or row counts.
- Do not perform production operations, role changes, tailnet changes, or trading
  resumption as part of this implementation.

## Artifact boundary

`--diagnostic` and `--classification` continue to accept `Path` values, but the
new values are private evidence directories rather than single large files:

```text
diagnostic/
  manifest
  COMPLETE
  chunks/00000000.part
  chunks/00000001.part

classification/
  manifest
  COMPLETE
  chunks/00000000.part
  chunks/00000001.part
```

The directory is created exclusively with mode `0700`. The manifest and part
files use mode `0600`; the `chunks` directory uses mode `0700`. An existing path,
symlink, incomplete directory, extra entry, wrong owner, or wrong mode is
rejected. A writer publishes `COMPLETE` only after all parts and the manifest
have been flushed and synchronized. A reader accepts an artifact only when all
required entries and `COMPLETE` are present.

An interrupted writer therefore leaves an unusable artifact rather than a
partially trusted one. The writer never overwrites an existing target. Cleanup
of a failed generated directory is limited to the exact directory created by
that writer; an operator may otherwise remove a known incomplete artifact before
retrying.

## Versioned wire format

Every manifest is an existing `encode_row` payload. Every part is a sequence of
frames:

```text
uint64_be(payload_length) + encode_row(record)
```

The first implementation uses these fixed resource limits:

- maximum encoded record payload: 1 MiB;
- maximum part bytes, including frame lengths: 4 MiB;
- maximum complete artifact bytes: 2 GiB;
- maximum part count: 4,096 parts; the total-size limit also applies, so an
  artifact must satisfy both limits.

The reader checks declared and observed byte counts, frame lengths, record
counts, lexical part ordering, and EOF/trailing bytes. It never allocates based
on an untrusted length without applying the record and artifact limits.

The diagnostic manifest has kind `projection-cutover-diagnostic-v2` and contains
exactly the following logical fields:

- `kind`, `format_version` (`2`);
- `run_id`, `scope`, `image_digest`, `projector_version`;
- `stream` identity;
- `tables` with the existing exact schema/key/count/digest facts;
- `record_kind` (`difference`), `record_count`, `total_bytes`;
- ordered part descriptors (`name`, `record_count`, `byte_count`, `digest`);
- `root_digest` and a manifest `digest`.

The classification manifest has kind
`projection-cutover-classification-v2` and contains the same cutover identity,
`diagnostic_digest`, `reviewer`, `record_kind` (`classification`), record count,
`total_bytes`, ordered part descriptors, `root_digest`, and manifest `digest`.

Each diagnostic record is the current complete `Difference` mapping. Each
classification record is the current complete mapping with `reason` and
`evidence`. Classification records must remain in the exact diagnostic order;
the verifier compares the binding fields one-for-one and does not use set
comparison to hide duplicate, missing, or reordered records.

Digest rules are domain-separated and deterministic:

- a part digest is SHA-256 of its exact frame bytes;
- `root_digest` is SHA-256 of the ordered concatenation of all frame bytes;
- manifest `digest` is SHA-256 of the canonical codec payload of all manifest
  fields except `digest` itself, including every part descriptor and
  `root_digest`.

The CLI's `--diagnostic-digest` and `--classification-digest` pin the respective
manifest digests. The prepared envelope remains the existing small
`projection-cutover-prepared-v1` container; its referenced diagnostic and
classification digests must resolve to v2 manifests. No large evidence bytes are
copied into the prepared envelope.

## Components and interfaces

Add a runtime-owned module under
`bfx_funding_bot.modules.execution.projection_cutover` for the artifact protocol.
It provides typed equivalents of:

- `ChunkDescriptor` and `EvidenceManifest`;
- a streaming `EvidenceWriter` with `append()` and `finish()` lifecycle;
- a bounded manifest/part iterator that verifies all bytes before yielding a
  record;
- `VerifiedCutoverEvidence`, a compact immutable summary for `apply`.

The summary includes the pinned diagnostic/classification manifest digests,
cutover identity, stream identity, record count, and bounded counts of the
allowed classification values. It is created only by the full streaming
verifier. `apply_cutover` does not accept a raw boolean or a caller-created
"verified" flag.

The shared replay kernel gains a typed, table-oriented streaming sink/source
boundary. It must expose source and rebuilt projection rows one table at a time
and use a bounded external spool or equivalent bounded merge so that Python
lists of all physical rows are not required. The existing small pure
`compare_rows` behavior remains available for unit tests and compatibility; the
production diagnostic path uses the streaming comparator.

`cutover_projection.py` changes as follows:

- `diagnose` writes diagnostic v2 chunks as differences are produced;
- classification validation streams diagnostic and classification artifacts in
  lockstep and returns `VerifiedCutoverEvidence`;
- `prepare` validates the full v2 artifacts outside the account-lock transaction,
  then binds only their compact digests/identity to the prepared envelope;
- `apply` rejects the old placeholder path and requires the v2 summary plus the
  reviewed archive-restore receipt.

`apply.py` receives the typed summary and rechecks its identity, digest, run,
scope, archive receipt, head, and freshness against the live transaction. It
does not read the evidence directory while holding the account lock. The
summary is evidence of the complete prior stream validation, not a replacement
for the in-transaction identity checks.

## Data flow

```text
source/rebuilt rows
        │  one table at a time
        ▼
bounded external spool + merge comparator
        │  one Difference at a time
        ▼
diagnostic v2 writer ──► diagnostic manifest digest
        │
reviewed classification v2 writer
        ▼
full lockstep verifier ──► VerifiedCutoverEvidence
        │                     (compact, digest-pinned)
        ▼
prepare / apply transaction checks
```

The diagnostic record order is deterministic: table name order, encoded key
order, then column order, matching the current difference semantics. The source
and rebuilt row streams are encoded before comparison so key ordering and row
identity do not depend on driver-specific Python representations.

Full artifact verification happens before the account lock and before any
mutation. Once verified, the compact summary is checked again inside the
transaction against the archive manifest, source stream identity, runtime
identity, classification policy, and current live projection evidence. The
account lock is therefore bounded by database checks and the actual apply, not
by hundreds of MiB of local file I/O.

## Failure and security behavior

The following conditions fail closed with a bounded error and no successful
receipt: malformed codec/frame, oversized record/chunk/artifact, missing or
extra part, wrong owner/mode, symlink, incomplete marker, digest mismatch,
manifest identity mismatch, duplicate/omitted/reordered record, unexplained
classification, unknown classification, or any classification not matching a
diagnostic record exactly.

Before append, any failure leaves source events and projections unchanged. After
append, rebuild, parity, or receipt insertion failures roll back the complete
transaction and must be tested against complete raw-row equality. Ambiguous
commit is resolved by the immutable receipt on restart. A post-commit failure
retains the new event/projection state and trading halt; it never restores old
archive rows automatically.

The artifact reader retains the existing single-file `read_private` protections
for prepared/operations files. Directory evidence gets equivalent owner,
permission, no-follow, exclusive-target, size, and digest checks without
loosening the old reader's 64 MiB rule for other file types.

## Testing and rollout boundary

Use `pytest` only. Add focused tests for:

- writer/reader round trips, empty artifacts, chunk boundaries and deterministic
  manifest/root digests;
- malformed frames, truncated/extra bytes, duplicate/missing/reordered records,
  size limits, extra entries, symlinks, owner/mode failures, and incomplete
  directories;
- v1 legacy inspection and v2-only prepare/apply enforcement;
- a synthetic 148,768-row missing-projection case proving bounded streaming and
  complete per-column record coverage;
- classification lockstep validation and digest binding;
- current PostgreSQL identity-map freshness, complete rollback equality, writer
  quiescence, and cross-account preservation;
- end-to-end diagnose → classify → prepare → apply → repeat apply with artifact
  tampering and ambiguous commit injection.

Implementation remains split under the Subagent-Driven Development workflow:

1. reviewed artifact protocol and bounded reader/writer;
2. reviewed streaming replay/diagnostic producer;
3. reviewed classification/prepare integration;
4. resumed Task 5 apply implementation and failure matrix;
5. Task 6 release verification and operator handoff.

No implementation, production migration, credential change, R2 operation, or
trading-resumption decision is authorized by this design alone.
