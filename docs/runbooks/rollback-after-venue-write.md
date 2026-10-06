# Rollback after a venue write

> Venue reality decides. Deploying, rolling back an image, or restoring a
> database never changes what Bitfinex holds. Deployment and image rollback are
> in the [deploy runbook](deploy.md); trading state, kill and uncertainty
> actions are in the [operations runbook](operations.md).

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
preserve the evidence bundle, and roll back the image through bfx-deploy
(automatic when no migration ran; see the deploy runbook) if the release itself
must be removed. Re-run read-only preflight, event-chain
replay, and a fresh full-account venue diff before considering any later
operator action.

**Operator confirmation:** record the image digest being rolled back, the
account-local event head/hash, the fresh snapshot fence, and the proof that no
venue write was attempted. Do not treat an image rollback as a resume. There
is no automatic retry.

### After any venue write

Once a submit may have reached the venue, a database image cannot establish the
venue's truth. Follow this sequence and do not skip a step:

1. Put and keep the account in `HALTED`: the UI kill switch (TOTP), or the
   emergency `POST /admin/halt` on the bot. Both commit `HALTED` and then run
   the venue funding cancel-all; require every currency's cancel-all to be
   `acknowledged` (UI panel or `funding_cancel_all_audit`), and repeat the kill
   until it is. HALTED is never lifted except by an operator resume in the UI.
2. Run a fresh full-account reconcile and record the event fence, timestamp,
   account-local projection hash, bounded venue IDs, and venue-vs-DB exposure
   diff.
3. Inspect the durable ledger attempts (`submission_attempt_journal` with their
   `transport_outcome_journal` outcomes) and open quarantines (`quarantine_opening`);
   the legacy `submission_attempts` / `execution_uncertainties` are archived
   (`legacy_archive`, read-only) and hold only pre-switch history. Bind an UNKNOWN submit to a venue object only through the audited
   `bind-to-venue` request when exactly one candidate matches; zero or multiple
   candidates remain UNKNOWN.
4. Use `mark-not-accepted` or `manual-resolution` only after complete, fresh
   reconcile evidence proves the chosen resolution. Do not retry, silently delete, or synthesize a venue
   reference.
5. Apply a forward-fix that preserves the append-only ledger journal (never edit or
   delete journal rows), then repeat reconcile and the final verification gates.
   Conversion/quarantine commands are separate explicit writes and each must return
   exit 0.

**Operator confirmation:** the named operator must sign the classification
(matched/adopted, still UNKNOWN, orphan quarantined, or manually resolved) and
the forward-fix evidence before the operator resumes in the UI under the applied
CapitalPolicy envelope. Keep the operator halt for UNKNOWN, orphan, mismatch, incomplete coverage, or any
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

Before resuming after the post-write branch, re-check: event-chain replay, no
open UNKNOWN or orphan, a fresh venue full-account diff of zero, and the
cancel-all outcome of the current HALTED. There is no automatic retry.

## Command and rehearsal contract

Repository commands above run from `backend/` (on the VM: as a one-shot of
the deployed backend image, see the deploy runbook). Exit 0 is a successful
read-only check or explicitly named append-only action; exit 2 is a
precondition refusal; exit 3 is verification unavailable or failed. Any other
exit, malformed output, or process crash is a stop. Rehearse the same sequence in the `ci` realm with the venue transport
fault matrix (accepted+drop, rejected+drop, timeout/reset, malformed response,
5xx-after-side-effect, and crash-during-send) before a production operator
uses it. The rehearsal must show one durable attempt, no retry, and a retained
halt for every ambiguous case.
