# Historical claim replay compatibility (Option A)

## Objective and approval boundary

Restore trading only after historical replay, disaster recovery, runtime safety,
and the operator canary gates pass. Option A is approved: preserve historical
events and current live identity enforcement; add narrowly scoped historical
replay compatibility. This document defines the implementation boundary for
review. Approval of a compatibility implementation is not evidence that any
production resume gate has passed.

## Evidence and root cause

The production event stream contains historical CID reuse: an intent, claim,
and fill for one venue offer are followed by another intent, claim, and fill
using the same CID but a different venue offer. The source replay reproduces
the isolated verifier's `claim identity conflict ... venue offer` failure.
Individual persisted event deserialization succeeds. The current CID-keyed
claim projector treats a venue binding as immutable for the entire CID, so it
cannot represent the old sequential lifecycle semantics during replay.

Physical recovery and replay acceptance are distinct: the diagnostic restore
completed recovery and removed R2 egress before the verifier failed. Neither
failed drill is accepted DR evidence.

## Alternatives

- **A, selected:** interpret provable historical lifecycle boundaries only in
  stored-event rebuild, retaining the existing final CID-keyed snapshot and
  all event contributions to positions and venue projections.
- **B, deferred:** introduce a new claim-cycle identity and migrate the claim
  schema and its consumers. This changes runtime contracts and requires a
  broader migration; it is not needed unless the historical evidence cannot
  be represented safely by A.

Do not rewrite CID/event payloads, discard older fills, skip failing events,
ignore projection differences, or relax live identity uniqueness.

## Compatibility boundary

Compatibility is authorized from persistent historical `event_log` rows via
the existing stored-event provenance boundary, not from a public boolean,
an environment flag, a caller-supplied event object, or `event_only_replay`
alone. New schema-v3/live submissions retain all current identity checks.

An account/environment-local replay context validates ordered lifecycle
boundaries before allowing the CID-keyed projection to start another cycle:

1. Events are read in increasing `event_seq` from the same immutable scope.
2. The preceding cycle is provably terminal from its own historical events.
   Absence of an offer in today's venue snapshot is not historical proof.
3. A subsequent historical `RESERVATION_INTENT` starts the next cycle. A
   changed venue ID on a claim/fill without that boundary is rejected.
4. CID, symbol, amount, and signal correlation agree within each cycle; venue
   attribution remains unique. Modern audited decision/attempt identities
   cannot acquire historical exceptions.
5. At the proven new intent only, the derived claim snapshot may reset to the
   new pending cycle and clear its previous venue binding. A later claim binds
   the new venue offer. Both cycles remain in the event stream and contribute
   exactly once to all applicable projections.
6. Overlapping cycles, unresolved/UNKNOWN predecessors, stale prior-cycle
   events after a new cycle begins, cross-scope reuse, contradictory identity,
   or incomplete evidence fail closed. Do not infer a missing boundary.

Use the same stored-row compatibility policy for full and symbol-scoped
rebuilds. Normal append must not be able to call a permissive alternative to
`_upsert_claim`. Replay context is per invocation and must not leak into a
subsequent append or another account. Prevalidate the stream before replacing
derived rows; retain the enclosing transaction's rollback semantics.

No schema migration or new projector version is planned: the supported
`execution-state-v1` gains a specified historical compatibility case, and
release evidence pins the implementing image. Canonical event identity and
hash serialization do not change.

## Code and tests

Expected code surface: `event_store/store.py` plus a focused internal
historical claim policy module if needed. Reuse existing stored provenance
validation instead of introducing another trusted identity decoder.

Tests must cover:

- A persisted synthetic legacy stream matching two completed cycles sharing
  one CID and using distinct venue offers; final claim is the latest cycle,
  both fills contribute, and original event count/hash/payloads are unchanged.
- The reset occurs at the new intent, not opportunistically at a conflicting
  claim. Repeated rebuilds yield identical counts/content hashes.
- A failed terminal cycle followed by a valid new cycle; exact duplicate
  observations retain existing idempotency semantics.
- Rejection of active or UNKNOWN predecessors, missing new intent, changing
  venue binding mid-cycle, changed scope/symbol/amount/correlation, modern
  identities, forged historical flags, and out-of-order prior-cycle events.
- Full and symbol-scoped rebuild policy parity, transaction rollback on a
  later conflict, and no compatibility leakage to live append.
- PostgreSQL temporary-schema replay with a least-privilege verifier role;
  diagnostic runtime tables are not an input to event-only reconstruction.
- All existing claim collision/concurrency and historical provenance tests.

Use pytest, not unittest. Run the complete non-integration backend suite,
targeted PostgreSQL replay/identity tests, mypy, ruff, and independent code
review before integration. Real historical events stay in private operator
evidence; repository fixtures use synthetic identifiers and amounts.

## Production validation and restoration sequence

1. Re-establish access to the correct VM tailnet without silently changing
   unrelated machine-global network/account state. Re-read halted/writer state.
2. Audit every known historical CID conflict against the boundary above. A
   case outside the design remains a blocker; no ad-hoc exception list.
3. Run the new projector on isolated data and compare every projection. If
   existing runtime projections differ, investigate each difference; do not
   modify expected baseline values to make acceptance pass. Any required
   production projection repair needs its own reviewed procedure and rollback.
4. While all writers remain stopped, produce fresh post-change R2 backup,
   independently captured canonical baseline, and isolated restore/replay.
   Verify image/config/schema identity, recovery completion, network isolation,
   and cleanup. Failed or stale evidence is not reusable acceptance.
5. Meet the approved Halt 2 RPO <=300 seconds and restore RTO <=3600 seconds,
   aligned with the existing operational target. A successful general DR drill
   with RTO <=3600 seconds does not by itself satisfy the canary gate. If
   measurement exceeds 3600 seconds, optimize and remeasure; changing that
   threshold is a separate operator decision, not part of Option A.
6. Validate runtime UUID/KEK and removal of legacy credential fallbacks,
   configuration, auth boundaries, halt effectiveness, no open uncertainty,
   and fresh full-account venue-vs-DB exposure reconciliation.
7. Follow the bounded operator canary procedure, then require two ordered,
   fresh reconcile cycles with zero exposure difference and consistent hashes.
   Only after passing evidence and explicit operator authorization may normal
   trading resume. Verify actual runtime trading health afterward.

## Completion and remaining uncertainty

Success means trading is actually resumed and verified after all gates, not
merely that compatibility tests pass. Current evidence does not establish a
resume time: all historical cases, projection parity, and the strict restore
RTO remain to be measured. Report newly discovered blockers explicitly.
