# Capital Authority Bounded Reads Implementation Plan

**Goal:** Make the per-decision capital read bounded by construction, and move full-history
re-derivation to the audit paths that already exist, without weakening any guarantee the
current full-scan provides.

**Architecture:** The existing `capital_snapshots` row is the materialized position; it gains a
binding to the ledger prefix it was derived from. The hot path verifies that binding on the
evidence row it already loads, then folds only intents after the snapshot's `command_fence`. Full re-derivation runs at snapshot acceptance, boot recovery
and periodic reconcile; divergence writes the durable halt. This is the pattern
`verify_release_preflight` already uses for release evidence, applied to capital.

**Tech Stack:** Python 3.13, Decimal, SQLAlchemy 2.0 async, Alembic, PostgreSQL 18, pytest.

## Why

Measured on production 2026-09-20 (see `## Evidence` below): `capital_policy` returns
`eval_timeout >2.0s` on every evaluation, so every offer is blocked regardless of halt state.
`_read_capital` loads and deserializes all 3,703 `RESERVATION_INTENT` rows twice per read, and
`_historical_cycles` loads the entire event log per read. The cost is O(total history) and grows
with every intent ever recorded; `GUARD_EVAL_TIMEOUT_SECONDS = 2.0` has now been crossed.

The failure mode matters as much as the cost: unbounded work behind a fail-closed timeout means
the system **silently converts to "block everything" as history grows**, with no signal that
anything degraded. Bounded work plus a timeout as a pathology backstop is the correct pairing.

## Guarantees that must survive

These are the properties the current full scan provides. Each must be preserved, not traded away.

1. **Immutable intents define the universe.** A missing or moved projection row is corruption,
   never evidence of released cash. Bidirectional intent ↔ attempt verification must remain.
2. **A cursor is not proof of completeness** (`wiki/tech/cache-not-source-of-truth.md`). The
   replacement binds the projection to a **content hash of the covered prefix**, not to a cursor.
   A projection that silently lost rows fails the hash and cannot authorize.
3. **Historical cycles require exact terminal evidence before the fence.** Partial fills and
   latest-mutable claims still cannot prove a terminal cycle.
4. **Fail closed.** Any unreadable, mismatched or absent projection blocks; it never degrades to
   an optimistic answer.

The invariant being restated is `_historical_cycles`' docstring, "never a cross-read cache".
Its purpose is (2). A prefix-hash-bound projection satisfies (2) strictly: it is re-verified
against the ledger on every read, it just does not re-derive from zero.

## Global Constraints

- Existing account, fUST only; fUSD disabled with full-account coverage preserved.
- Decimal native arithmetic; no NaN/Infinity/negative amounts.
- Preserve persistent halt, UNKNOWN fail-closed, once-only submits, account/realm isolation,
  audit-before-submit, single writer.
- Apply migrations only with `cd backend_py && uv run alembic upgrade head`; never MCP SQL.
- Unit tests use pytest. Run affected tests while iterating and the full non-integration suite
  before each commit. Database behavior needs the real PostgreSQL integration fixture (Docker);
  a teardown error is not a clean pass.
- Agent may implement and technically deploy with halt retained. Agent must not submit
  real-money lending, consume a live permit, or resume lending. Activation is a human action.
- Worktree `.worktrees/capital-policy-release`, branch `codex/operator-onboarding`.

## Stable interfaces

### Snapshot binding (no new table)

`capital_snapshots` already materializes the position: `classification` carries the
per-symbol available/offered/credits/cells and the `reflected` set, and the row already
carries `event_seq` and `command_fence`. Reading it costs one indexed row today. What is
missing is only the binding to the ledger prefix it was derived from.

Add to `capital_snapshots`:

| column | meaning |
|---|---|
| `covered_prefix_hash` | `event_log.prefix_hash` of the snapshot's evidence event at acceptance |

Verification is free: `_read_capital` already loads that evidence row
(`session.get(EventLogRow, row.event_seq)`), and the row now carries its prefix hash. A
mismatch means the prefix the snapshot was derived from is no longer the prefix in the
ledger, which blocks.

Rejected: a separate `capital_positions` table. It would duplicate `classification` and
introduce a second thing that can disagree with the snapshot, which is the failure mode
this work exists to remove.

### `read_capital` hot path

Intents at or before `command_fence` cannot contribute: the loop either skips them
(already reflected) or refuses (`unclassifiable_commitment`). Scanning them detects
unreflected history, which is an integrity question, not an authorization one. So the
hot path folds only `event_seq > command_fence`:

```python
logged = await session.get(EventLogRow, row.event_seq)      # already done today
if logged.prefix_hash != row.covered_prefix_hash:
    raise CapitalBlockedError("snapshot_prefix_diverged")
tail = await intents_after(session, row.command_fence)      # normally 0..few rows
snapshot = fold_tail(row.classification, tail, symbol=symbol, cell_id=cell_id)
```

`_historical_cycles` and the full intent scan move to the audit path (Task 4). They run
once at acceptance, when the fence is set, and continuously in reconcile.

### Divergence

`CapitalBlockedError("snapshot_prefix_diverged")` and any audit-path mismatch are reported to
the existing halt store with actor `reconcile`, reason `capital_position_diverged:<detail>`.
Divergence is a halt condition, not a retry condition.

## Task 1: Rolling prefix hash so verification is O(1) — **done**

**Files:** `backend_py/src/bfx_funding_bot/modules/execution/event_store/canonical.py`,
`.../event_store/writer.py`, `backend_py/tests/modules/execution/test_canonical.py`.

`canonical_event_hash(rows)` currently hashes a whole sequence, so verifying a prefix would
itself be O(history). Add a rolling form persisted on `event_log`: each appended row stores
`prefix_hash = H(previous_prefix_hash || canonical_event_record(row))`. Verifying a prefix then
reads one row.

- [ ] RED: test that `prefix_hash` on the last row of a sequence is reproducible from
      `canonical_event_record` alone, and that reordering or mutating any earlier row changes it.
- [ ] RED: test that the rolling chain agrees with `canonical_event_hash` semantics — equal
      streams produce equal chains, any difference produces a different chain.
- [ ] GREEN: add the column + backfill migration; `AccountEventWriter.append`/`append_batch`
      compute it under the existing single-writer guarantee.
- [ ] Keep `canonical_event_hash` unchanged; `verify_release_preflight` must keep passing
      untouched. Do not redefine the release evidence contract in this task.

**Caution:** the backfill must be deterministic and run inside the migration, not lazily at
runtime. A NULL `prefix_hash` must block, never be treated as "not yet computed".

## Task 2: Persist the capital position at snapshot acceptance — **done**

**Files:** `.../execution/capital_tables.py`, `.../execution/capital_repository.py`,
`backend_py/alembic/versions/`, `backend_py/tests/integration/test_capital_repository.py`.

`accept_snapshot` already runs under `_prepare` (writer lock), already validates the observation
and already computes the classification. Extend it to record `covered_prefix_hash` from the
evidence event's `event_log.prefix_hash` (**done**, commit 68e88cb), and to run the full
historical validation once here, where the fence is set (**not done, and Task 3 is unsafe
without it**).

- [ ] RED: a read after acceptance returns exactly what the current full-scan `_read_capital`
      returns for the same state (differential test against the old implementation, which stays
      available as `_read_capital_full` for Task 4).
- [ ] RED: a snapshot whose `covered_prefix_hash` does not match the evidence row blocks with
      `snapshot_prefix_diverged`.
- [ ] RED: a read path cannot write the binding — reads use a session without that privilege
      (restricted role test, following `test_release_migration.py`'s pattern).
- [ ] GREEN: implement, with migration via `alembic revision --autogenerate` then reviewed by hand.

## Task 3: Bounded hot-path read — **done**

> **Done** (f922b2f). The read folds only commitments after the accepted fence.
> `test_capital_read_work_does_not_grow_with_history` went from 25 vs 769
> deserializations across a 32x history increase to bounded, its strict xfail flipped to
> XPASS, and the marker is removed.
>
> The attempt-side narrowing that blocked two earlier attempts is solved by
> `classification["settled"]`: `_classify`'s existing loop already calls
> `_effective_outcome` and skips `rejected`/`not_sent`, so recording those keys costs
> nothing at acceptance, and the read narrows by `reflected | settled` -- set membership,
> no per-attempt work. Deriving it in the read is what put the unbounded work back and
> broke `test_current_cursor_cannot_hide_durable_commitment`.

**Files:** `.../execution/capital_repository.py`, `.../execution/capital_runtime.py`,
`backend_py/tests/integration/test_capital_repository.py`.

Replace `_read_capital`'s full scans with the `## Stable interfaces` sequence. The tail fold must
handle exactly the event kinds that can change capital after a fence:
`RESERVATION_INTENT`, `RESERVATION_CLAIMED`, `RESERVATION_FAILED`, `ORDER_FILL`,
`RESERVATION_RELEASED`, `SUBMIT_OUTCOME_UNKNOWN`, `SUBMIT_MATCHED_TO_VENUE_OFFER`.

- [ ] RED: query-count and deserialization-count assertions — a read issues a bounded number of
      queries and deserializes only tail events, independent of total history size. Build the
      fixture with a large synthetic history so the test would fail on the current implementation.
- [ ] RED: bidirectional intent ↔ attempt verification still rejects a tail attempt whose amount,
      decision scope or account/realm disagrees (port the existing cases).
- [ ] RED: an intent at or before the fence that is not reflected still raises
      `unclassifiable_commitment`.
- [ ] RED: UNKNOWN outcomes in the tail still fail closed.
- [ ] GREEN: implement; measure a read against the production-sized history and record the number.

## Task 4: Move full re-derivation to the audit path — **re-scoped, mostly obsolete**

**Files:** `.../execution/periodic_reconcile.py`, `.../execution/boot_recovery.py`,
`.../execution/capital_repository.py`, `backend_py/tests/integration/`.

Written before Task 2 landed, and Task 2 landed somewhere better than planned. Reconcile
already calls `accept_snapshot` roughly every 90s, and acceptance now runs the full
historical settlement proof and re-derives the classification from scratch. **The audit
path is already running continuously; it is acceptance.** Adding a second full
re-derivation to the reconcile tick would repeat that work, and would need a halt store
threaded through `BootRecovery`, which has none.

What is genuinely missing is narrower: acceptance *re-derives*, it does not *compare*. A
drift check would carry the previous snapshot's accounting forward over the tail and
require it to equal the freshly derived classification. That is a real runtime
differential, it needs no new dependency because both values are in hand inside
`accept_snapshot`, and a mismatch is a halt condition.

Bounding the residual risk honestly: the bounded read and the full re-derivation share a
basis and differ only in which commitments they fold, and everything before the fence is
proved accounted at acceptance. `_assert_no_unknown` blocks acceptance while an
uncertainty is open, so an unresolved attempt cannot be inside an accepted state. The
window for divergence is therefore one reconcile interval, and the CI differential test
pins that the two folds agree. This is defence in depth, not a gap.

- [ ] Compare carried-forward accounting against the freshly derived classification inside
      `accept_snapshot`, and write the durable halt with actor `reconcile` and reason
      `capital_position_diverged:<detail>` on mismatch.
- [ ] RED first: an injected classification edit must be detected and must halt.
- [ ] Do **not** add a second full re-derivation to the reconcile tick.

## Task 5: Guard timeout becomes a backstop, not a correctness boundary — **partly done**

**Files:** `.../execution/safety/chain.py`, `.../execution/safety/hard_guards.py`,
`backend_py/tests/modules/execution/`.

- [ ] RED: a guard whose work is unbounded is a test failure — assert `capital_policy` issues a
      bounded query count, rather than asserting it finishes within a wall-clock budget.
- [ ] RED: an eval timeout still fails closed and still surfaces a named reason.
- [x] GREEN: `GUARD_EVAL_TIMEOUT_SECONDS` stays a pathology backstop, and a guard that uses
      `GUARD_EVAL_WARN_FRACTION` of its budget logs `guard_slow name=... elapsed=... budget=...`
      while still allowing. Two regressions pin it: a slow guard reports and still allows, a fast
      one stays quiet, because a signal that fires often means nothing.
- [ ] Still open: the bounded-work assertion for `capital_policy` lives in
      `test_capital_read_work_does_not_grow_with_history`, which counts deserializations. Wiring
      the same idea into the guard layer (assert query/work counts rather than wall clock) is not
      done.

**Rationale:** today's incident had no warning signal. The only symptom was total blockage.

## Task 6: Rewrite affected regressions and contracts — **done**

**Files:** `backend_py/tests/integration/test_capital_repository.py`,
`backend_py/ARCHITECTURE.md`, `AGENTS.local.md` (ignored, personal lookup section).

`test_historical_capital_read_has_bounded_scope_queries_and_validation` and the historical-cycle
tests currently assert *per-read full validation*. Their intent — completeness cannot be assumed —
must be re-expressed against the new contract, not deleted.

- [x] The rewritten tests name the guarantee they pin, and the bounded-scope test now pins that
      full validation is renewed per acceptance and never per read, with the reason it cannot be
      per read: its scope is all of history.
- [x] `ARCHITECTURE.md` documents the authorization/audit split, the three facts acceptance
      persists, why the prefix hash does not cover the historical proof, and why an unsettled
      history records a verdict instead of refusing the observation.
- [x] `AGENTS.local.md` no longer tells the next reader to verify per read and never cache across
      reads -- that guidance became wrong the moment completeness moved to a prefix hash. It now
      names both traps hit on the way here.

## Deployment note

This ships through the normal release path. Two things learned on 2026-09-20 apply:

- `prepare` requires `DOCKER_BUILDKIT=0`; BuildKit's default attestations produce a nested
  `index.json` that `inspect_archive` rejects as `invalid_image_archive`.
- **Prove the guard chain passes with `POST /admin/dry-evaluate` before opening a DR window.**
  The 900s DR receipt window is expensive and today three were spent on a failure that
  dry-evaluate surfaces in ten seconds.

## Evidence (2026-09-20, production)

- `POST /admin/dry-evaluate` → `capital_policy allowed=False internal_error=True
  reason=eval_timeout >2.0s`; the other seven guards pass.
- During evaluation `pg_stat_activity` shows the guard's connection `idle in transaction /
  ClientRead` — PostgreSQL is finished and waiting on the application.
- `docker stats`: `bfx-bot` saturates one core (~100%, idle baseline 0.8%); `bfx-postgres` ~0%.
- `event_log`: `RESERVATION_INTENT` 3,703, `RESERVATION_FAILED` 3,625,
  `VENUE_SNAPSHOT_OBSERVED` 529; max payload 1,918 bytes; `submission_attempts` empty;
  host load average 0.57 on 2 cores.

The cost is neither the database, the payload size, nor the host — it is 3,703 Python
deserializations per read, twice.

## Final verification handoff

- [ ] `cd backend_py && uv run ruff check && uv run mypy src/`
- [ ] `cd backend_py && uv run pytest -m "not integration"` fully green
- [ ] `cd backend_py && uv run pytest tests/integration/test_capital_repository.py` against real
      PostgreSQL, green, no teardown errors
- [ ] `cd backend_py && uv run alembic upgrade head && uv run alembic check` (no drift)
- [ ] Measured: capital read query count and elapsed time against production-sized history,
      recorded in the PR body
- [ ] `POST /admin/dry-evaluate` on the deployed candidate shows `capital_policy allowed=True`
      (or a named business reason), with no `internal_error`
- [ ] Independent review before the human activation step
