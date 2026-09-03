# Rollback after a Halt 2 venue write

This is an operator decision runbook. The coding agent does not execute production operations: no remote migration, no deployment, no restart, no
resume, no cap increase, and no Bitfinex write. Keep reports redacted: never
record an API key, secret, Authorization header, or complete raw response.

## First decision: has any venue write happened?

**Operator confirmation:** use the durable intent/outcome record and a fresh
full-account venue observation to select exactly one branch. Do not infer a
negative from a timeout, a missing response, or an old database projection.

### Before any venue write

This branch applies only with affirmative evidence that no submit request
started and no later venue mutation occurred. Keep the persistent halt set,
preserve the evidence bundle, and use an immutable image rollback if the
cutover image itself must be removed. Re-run read-only preflight, event-chain
replay, and a fresh full-account venue diff before considering any later
operator action.

**Operator confirmation:** record the image digest being rolled back, the
account-local event head/hash, the fresh snapshot fence, and the proof that no
venue write was attempted. Do not treat an image rollback as a resume. There
is no automatic retry.

### After any venue write

Once a submit may have reached the venue, a database image cannot establish the
venue's truth. Follow this sequence and do not skip a step:

1. Assert and retain the persistent halt. From `backend_py/`, run
   `uv run python scripts/halt2_cutover.py assert-halt` with the approved
   account/environment/evidence arguments. Require exit 0; a nonzero exit is
   a stop and the process must not be restarted into an active state.
2. Run a fresh full-account reconcile and record the event fence, timestamp,
   account-local projection hash, bounded venue IDs, and venue-vs-DB exposure
   diff.
3. Inspect the durable `canary_command_permit` and `submission_attempts` rows;
   a consumed permit is never reset or reused. Adopt an exactly matched venue
   object only through the audited adopt path;
   zero or multiple candidates remain UNKNOWN.
4. Use manual resolution only after complete, fresh reconcile evidence proves
   the chosen resolution. Do not retry, silently delete, or synthesize a venue
   reference.
5. Apply a forward-fix that preserves the append-only event chain and rebuild
   the projection from events with `uv run python scripts/verify_projection_replay.py
   replay ...`; require exit 0, then repeat reconcile and the final verification
   gates. The replay command is read-only; conversion/quarantine commands are
   separate explicit writes and each must return exit 0.

**Operator confirmation:** the named operator must sign the classification
(matched/adopted, still UNKNOWN, orphan quarantined, or manually resolved) and
the forward-fix evidence before any separately authorized next step. The halt
remains effective for UNKNOWN, orphan, mismatch, incomplete coverage, or any
nonzero exposure difference. There is no automatic retry.

## Database restore rule

DB restore is allowed only with proof that no later venue mutation occurred.
That proof must be measured, account-scoped, and timestamped after the restore
point: it includes the last known venue-write boundary, fresh full-account
snapshot coverage, a reconciled venue-object inventory, event head/hash, and a
recorded zero-mutation interval. An assumption that a restore point predates a
venue write is not proof.

If a venue write may have happened after the candidate restore point, do not
restore the DB. Use halt, reconcile, adopt, manual resolution, and forward-fix
instead. The coding agent does not perform a DB restore or any production
operation.

## Required rollback evidence

Record only bounded/redacted facts: account UUID and environment, operator ID,
image/config/projector digests, backup and isolated-restore hashes, event
head/hash, snapshot fence/timestamp, reconcile fences, projection hash, venue
IDs, UNKNOWN/orphan classification, exposure diff, stop reason, and whether a
later venue mutation was disproved. A mismatch, incomplete snapshot, unresolved
UNKNOWN, orphan, missing proof, or nonzero diff is a stop condition.

The post-write branch returns to the final checks in the [Halt 2 canary
runbook](halt-2-projector-canary.md): API auth denial, direct signup denial,
event-chain replay, UNKNOWN fault matrix, orphan quarantine, fresh venue
full-account diff, persistent halt effectiveness, and no automatic retry.

## Command and rehearsal contract

All commands above run from `backend_py/` unless they explicitly begin with
`cd backend_py`; `deploy-vm.sh` runs from the repository root. Exit 0 is a
successful read-only check or explicitly named append-only action; exit 2 is a
precondition refusal; exit 3 is verification unavailable or failed. Any other
exit, malformed output, missing durable permit binding, or process crash is a
stop. Rehearse the same sequence in the `ci` realm with the venue transport
fault matrix (accepted+drop, rejected+drop, timeout/reset, malformed response,
5xx-after-side-effect, and crash-during-send) before a production operator
uses it. The rehearsal must show one durable attempt, no retry, and a retained
halt for every ambiguous case.
