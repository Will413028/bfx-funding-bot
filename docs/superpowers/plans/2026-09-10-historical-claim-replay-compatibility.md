# Historical Claim Replay Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement approved Option A and satisfy the remaining operational gates before restoring trading.

**Architecture:** Prevalidate persistent historical events into a per-rebuild cycle policy. At a proven terminal-to-new-intent boundary, reset only the derived legacy claim, then use the existing strict projector. Live append, stored event bytes, canonical hashes and modern identities remain unchanged.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest, PostgreSQL 18.6, pgBackRest/R2.

**Spec:** `docs/superpowers/specs/2026-09-10-historical-claim-replay-compatibility-design.md`

## Global Constraints

- Do not rewrite CID/event payloads, discard older fills, skip failing events, ignore projection differences, or relax live identity uniqueness.
- New schema-v3/live submissions retain all current identity checks.
- Compatibility is authorized from persistent historical `event_log` rows via the existing stored-event provenance boundary, not from a public boolean, an environment flag, a caller-supplied event object, or `event_only_replay` alone.
- Use the same stored-row compatibility policy for full and symbol-scoped rebuilds.
- Canonical event identity and hash serialization do not change.
- Use pytest, not unittest.
- Meet the approved Halt 2 RPO <=300 seconds and restore RTO <=3600 seconds,
  aligned with the existing operational target.
- Do not resume before fresh DR, config/auth/exposure and bounded-canary evidence passes.

### Task 1: Stored-row cycle validation and replay integration

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/event_store/historical_claims.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py`
- Create: `backend_py/tests/modules/execution/event_store/test_historical_claim_cycles.py`
- Modify: `backend_py/tests/integration/test_halt2_replay_cutover.py`

**Interfaces:**
- Consumes: persisted `EventLogRow`, `deserialize_stored_event`, `PostgresEventStore.rebuild_snapshot_from_log` and the approved spec.
- Produces: internal `historical_claim_reset_sequences(rows: Sequence[EventLogRow], *, account_id: str, environment: str) -> frozenset[int]` returning only proven reset-intent sequence numbers after whole-stream validation. Caller creates a new policy result for every rebuild. It must reject invalid provenance or lifecycle transitions before any projection deletion. No live append caller receives this policy.

- [ ] Write failing SQLite persisted-event fixtures with a canonical UUID, real ORM persistence, synthetic CID 81, signal UUID, amount 5, and event sequence `INTENT/CLAIMED/ORDER_FILL/INTENT/CLAIMED/ORDER_FILL`, venue IDs `old-a` and `old-b`. Reuse `_create_all` setup patterns in `test_store_append_unit.py`, not mocked store behavior. Parameterize full and symbol-scoped rebuilds. Assert:

  ```python
  before_hash = canonical_event_hash(rows)
  await store.rebuild_snapshot_from_log(session, account_id=account, deployment_environment="prod", symbol=symbol)
  claim = (await session.scalars(select(OfferClaimRow))).one()
  assert claim.state == "released"
  assert claim.venue_offer_id == "old-b"
  assert canonical_event_hash(rows) == before_hash
  assert len(rows) == 6
  ```

  Add projection assertions proving both historical fills contribute and repeated rebuilds produce identical fields. Preserve fixture payload copies and compare after rebuild. A fresh snapshot later in the stream may legitimately supersede exposure; do not substitute runtime projections for replay inputs.
- [ ] Run `cd backend_py && uv run pytest tests/modules/execution/event_store/test_historical_claim_cycles.py -q`. Record the expected venue identity conflict before implementation.
- [ ] Implement the policy in `historical_claims.py`: validate row scope/order/persistence and decoded provenance, track per-CID cycle identity/state and per-venue attribution across the stream, and emit reset sequence only for a new historical intent following a provably terminal historical cycle. Reject overlap, UNKNOWN, missing intent, older-cycle delivery, identity contradictions or mixing modern identities into a reused historical CID. Simple historical streams without reuse keep existing semantics. Within-cycle identity fields must match; a new proven cycle may carry its own amount/correlation. Do not guess that a partial fill is terminal merely from its event name: validate the historical claim/fill amount semantics and refuse unproven completion.
- [ ] Integrate once in each rebuild path, after reading scoped rows and before deleting derived rows. The reset runs only at an authorized sequence, removes the preceding derived CID claim, and lets the unchanged strict `_upsert_claim` create the new intent. All older events have already contributed to position/venue projections; do not remove them or remap their CID. Keep active-path strict SQL arbitration unchanged. Core reset shape:

  ```python
  reset_sequences = historical_claim_reset_sequences(rows, account_id=account_id, environment=deployment_environment)
  # In the scoped persistent-row rebuild loop only:
  if row.event_seq in reset_sequences:
      await session.execute(delete(OfferClaimRow).where(
          account_scope_clause(session, account_id=account_id,
              exchange_account_column=OfferClaimRow.exchange_account_id,
              legacy_account_column=OfferClaimRow.account_id),
          OfferClaimRow.deployment_environment == deployment_environment,
          OfferClaimRow.cid == row.cid,
      ))
      await session.flush()
  ```

  Use a small shared private helper to avoid duplicate reset code in full/symbol rebuild. Do not add a permissive boolean argument to `_upsert_claim`. Do not keep policy state on a reusable store object.
- [ ] Extend pytest matrix with: failed-to-new cycle, active/UNKNOWN predecessor, missing new intent, conflicting venue mid-cycle, duplicate venue across CIDs, cross-account/environment rows, forged/transient historical rows, modern schema-v3 lifecycle, changed intra-cycle amount/symbol/correlation, stale prior-cycle events, rollback after later conflict, no leakage into subsequent strict append, and repeated rebuild identity/hash stability. Exact duplicate behavior must agree with existing idempotency rules rather than double-count contributions.
- [ ] Add PostgreSQL integration test using the existing `pg_session_factory` and `replay_one_account` path: temporary projection reconstructs both cycles; source event count/hash are stable; runtime projection mutations do not change replayed results. Include least-privilege replay coverage and prove an invalid stream does not alter runtime rows. Reuse existing DR role bootstrap fixture where available; no production credentials in tests.
- [ ] Run targeted tests, existing claim/provenance tests, PostgreSQL integration, full non-integration suite, ruff and mypy. Stop on regressions; preserve RED/GREEN outputs in the private task report. Run full suite once per final implementation revision, not each edit.

  ```bash
  cd backend_py
  uv run pytest tests/modules/execution/event_store -q
  uv run pytest tests/integration/test_halt2_replay_cutover.py -m integration -q
  uv run pytest -m "not integration" -q
  uv run ruff check
  uv run mypy src/
  ```

- [ ] Commit scoped implementation/tests only using the repository commit hook. Controller dispatches independent spec/quality review and resolves findings before acceptance.

### Task 2: Operational replay and trading restoration gates (controller/operator)

**Files:** private operator evidence only; follow `docs/runbooks/offsite-dr.md`, `halt-1-exchange-account-cutover.md`, `halt-2-projector-canary.md`.

**Interfaces:** consumes reviewed Task 1 release image and existing private source manifest/baseline; produces measured restoration evidence and finally verified resumed trading, not a synthetic success report.

- [ ] Verify correct personal tailnet, both peers reachable, source halt=true, writers stopped and unchanged event head. Explicit user approval to switch to personal Tailscale was received; do not switch GitHub accounts.
- [ ] Privately audit all source historical CID collisions against Task 1 boundaries. Preserve exact source inputs and reason codes. If any conflict is unprovable, halt compatibility rollout and investigate; never add ad-hoc exceptions.
- [ ] Build reviewed release on DR; restore into generated isolated resources, disconnect egress after recovery, and run actual same-target replay. Preserve count/hash/projection comparisons and cleanup evidence. Investigate mismatch rather than changing expected baseline.
- [ ] When necessary, design a separately reviewed projection repair before touching production projections. The approved Option A does not authorize rewriting immutable history.
- [ ] With source writers halted, create fresh R2 backup/preflight and canonical source baseline, then rerun isolated restore with the matching target. Require measured RPO <=300 seconds and RTO <=3600 seconds; diagnose performance or any replay error without lowering the threshold.
- [ ] Validate runtime UUID/KEK, removal of legacy secret fallbacks, config/image identities, API auth denial, no unresolved UNKNOWN/orphan, persistent halt and fresh full-account venue exposure parity.
- [ ] Perform bounded canary only within explicit operator authority; require two increasing fresh reconcile fences/timestamps with zero exposure difference and stable projection evidence. Resume only after the complete gate passes and verify running daemon health and intended trading operation. Retain last safe rollback evidence.
- [ ] Report actual completion or precise remaining blockers. Do not equate Task 1 completion or a physical restore with trading restored.
