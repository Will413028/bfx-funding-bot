# Halt 2 projector cutover and bounded canary

> Historical evidence: the env-scoped permit/canary/deploy commands below are
> superseded and must not be run for capital-policy releases. Use
> [immutable release](immutable-release.md) and its authenticated human session
> workflow. Existing Halt2 evidence/DR invariants remain authoritative; historical
> observations are retained here, not new release authorization.

This is an operator runbook. The coding agent does not execute production operations.
Its prohibited actions are: no remote migration; no deployment; no restart; no resume; no cap increase; and no Bitfinex write. A named production operator
performs each confirmation point below and records only redacted evidence.

## Scope and non-negotiable stop conditions

Keep the persistent halt in place throughout this runbook. Stop / abort and
leave it in place for any nonzero result, changed account/environment identity,
an incomplete snapshot, nonzero projector lag, an open uncertainty, an
unmatched hash, an orphan, a second command/symbol/cell, a cap change, or a
venue-vs-DB exposure difference. Do not retry an ambiguous venue request: the
safe outcome is UNKNOWN and no automatic retry.

Never place an API key, secret, Authorization header, or complete raw response
in a report, shell history, ticket, or example. Keep only a hash, bounded
reason, event sequence, venue ID, and timestamp.

## Required evidence and preflight gates

The evidence file is the redacted Halt 2 evidence JSON. The freshly printed
preflight report must contain every field below and must agree with the
immutable evidence where applicable.

| Field | Gate |
| --- | --- |
| `exchange_account_id`, `deployment_environment` | Exact account UUID and deployment realm; no default identity. |
| `migration_head`, `schema_heads` | Expected Alembic/schema heads. |
| `backup_evidence_hash`, `isolated_restore_evidence_hash` | Hashes prove measured backup and isolated restore evidence. |
| `event_count`, `event_head`, `event_hash` | Account-local event-chain continuity. |
| `open_uncertainty_count`, `venue_snapshot_fence`, `venue_snapshot_observed_at_ms`, `venue_snapshot_complete` | No unresolved submit, a fresh full-account snapshot fence, and complete venue coverage. |
| `preflight_observed_at_ms`, `backup_rpo_seconds`, `restore_rto_seconds` | Preflight freshness plus measured RPO ≤ 300s and RTO ≤ 3600s. |
| `config_digest`, `image_digest`, `projector_version` | Exact immutable build/config/projector inputs. |
| `persistent_halt`, `stop_reasons` | Halt is durable and every refusal is explicit. |

The following `stop_reasons` are hard gates, not warnings:
`legacy_environment_variable:BFX_ACCOUNT_ID`, `missing_evidence`,
`exchange_account_id_mismatch`, `deployment_environment_mismatch`,
`backup_evidence_hash_mismatch`, `isolated_restore_evidence_hash_mismatch`,
`event_head_mismatch`, `event_hash_mismatch`, `config_digest_mismatch`,
`image_digest_mismatch`, `migration_head_mismatch`, `schema_heads_mismatch`,
`projector_version_mismatch`, `venue_snapshot_fence_absent`,
`venue_snapshot_timestamp_absent`, `venue_snapshot_coverage_incomplete`,
`venue_snapshot_stale`, `backup_rpo_unmeasured`, `backup_rpo_exceeded`,
`restore_rto_unmeasured`, `restore_rto_exceeded`, `open_execution_uncertainty`,
and `persistent_halt_absent`. Identity, permit, and outcome failures from the
canary verifier are also hard stops; they are never bypassed by editing JSON.

`halt2_cutover.py` returns exit 0 only when its selected action succeeds, exit
2 for a precondition failure (including the hard gates above), and exit 3 when
verification is unavailable or fails. A nonzero exit means stop / abort; do
not bypass it with an environment override.

## Exact command sequence

Run the following from the repository root unless the command first changes to
`backend`. Replace angle-bracket values only with the already approved,
account-scoped production evidence; do not print their contents. The commands
are deliberately ordered and every **Operator confirmation** is a hard pause.

1. **Operator confirmation — identity and immutable inputs.** Confirm the
   account UUID, environment, operator ID, evidence paths, image digest,
   projector version, and config artifact were independently reviewed. Reject
   `BFX_ACCOUNT_ID`; use `BFX_EXCHANGE_ACCOUNT_ID` only.

   ```bash
   cd backend
   uv run python scripts/halt2_cutover.py assert-halt \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --evidence "$BFX_HALT2_EVIDENCE_REPORT" \
     --projector-version "$BFX_PROJECTOR_VERSION" \
     --image-digest "$BFX_EXPECTED_IMAGE_DIGEST" \
     --operator-id "$BFX_OPERATOR_USER_ID" \
     --reason "Halt 2 projector cutover"
   ```

   Require exit 0. This is the only `halt2_cutover.py` action here that writes:
   it sets/asserts the durable `trading_halt` through the existing halt state
   path; it neither resumes nor calls Bitfinex.

2. **Operator confirmation — read-only evidence is complete.** With the halt
   confirmed, run the read-only preflight.

   ```bash
   uv run python scripts/halt2_cutover.py preflight \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --evidence "$BFX_HALT2_EVIDENCE_REPORT" \
     --projector-version "$BFX_PROJECTOR_VERSION" \
     --image-digest "$BFX_EXPECTED_IMAGE_DIGEST" \
     --config-artifact <reviewed-config-artifact-path>
   ```

   Require exit 0 and archive the redacted output. Exit 2 or exit 3 is a stop
   / abort condition. Do not alter the evidence to make a mismatch disappear.

3. **Operator confirmation — event-only replay.** Use the real projector CLI;
   it creates an empty temporary projection and treats the old projection only
   as a diagnostic comparison.

   ```bash
   uv run python scripts/verify_projection_replay.py replay \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --projector-version "$BFX_PROJECTOR_VERSION" \
     --expected-event-hash <event_hash-from-preflight>
   ```

   Require exit 0 and identical account-local `event_hash`/projection content
   hashes across the reviewed repeat. `uv run python scripts/halt2_cutover.py replay` is intentionally a reserved verb and returns exit 2; never replace
   the `verify_projection_replay.py` command with it.

4. **Operator confirmation — conversion and quarantine.** Proceed only after
   the replay report is accepted and a human has approved the recorded UTC
   epoch milliseconds. These commands append events through
   `AccountEventWriter`; they never submit a venue request or release the halt.

   ```bash
   uv run python scripts/verify_projection_replay.py convert-pending \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --now-ms <approved-utc-epoch-milliseconds>
   uv run python scripts/verify_projection_replay.py quarantine \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --now-ms <approved-utc-epoch-milliseconds>
   ```

   Require exit 0 for each. A converted PENDING becomes UNKNOWN; an orphan is
   quarantined with its venue object retained. Any resulting open uncertainty
   keeps the persistent halt effective.

5. **Operator confirmation — issue the one-shot permit.** After replay,
   conversion, and quarantine are accepted, issue exactly one durable permit
   for one account, one symbol, and one cell/strategy. This writes only the
   scoped `canary_command_permit` row; it does not resume the scheduler, call
   Bitfinex, or infer authority from an evidence JSON file. Record the returned
   UUID as `BFX_CANARY_PERMIT_ID` in the protected canary environment:

   ```bash
   uv run python scripts/issue_canary_permit.py \
     --account-id "$BFX_EXCHANGE_ACCOUNT_ID" \
     --environment "$BFX_DEPLOYMENT_ENV" \
     --symbol "$BFX_CANARY_SYMBOL" \
     --cell "$BFX_CANARY_CELL" \
     --strategy "$BFX_CANARY_STRATEGY" \
     --amount-usdt "$BFX_CANARY_AMOUNT_USDT" \
     --operator-id "$BFX_OPERATOR_USER_ID"
   export BFX_CANARY_PERMIT_ID=<returned-durable-permit-uuid>
   ```

   Require exit 0. A permit is tied to the current durable halt epoch and is
   consumed once, committed before the venue boundary. A second issue for the
   same halt is rejected.

6. **Operator confirmation — bounded canary command and deployment.** Under a
   separately granted production procedure, the live process admits exactly
   one minimal command for the permit's account, symbol, and cell. The daemon's
   pre-command verifier reads the durable permit and fresh readiness facts; it
   does not consume a post-command JSON report. The one-shot gate consumes the
   permit immediately before the executor and reasserts the persistent halt in
   every terminal path. An ACK, rejection, malformed response, timeout, or
   process crash never authorizes a retry. After the command, it must create
   one durable outcome and then two full-account reconcile cycles. It cannot auto-ramp; no report can increase a cap or expand symbols. Start a separate shell session at the repository root and deploy only after the separate authority confirmation:

   ```bash
   cd backend
   cd ..
   BFX_CANARY_CONFIRM=yes ./scripts/deploy-vm.sh canary
   ```

   Require the deployment command's exit 0 and then verify the server-derived,
   redacted evidence read-only:

   ```bash
   uv run python scripts/run_canary_preflight.py \
     --evidence "$BFX_CANARY_EVIDENCE_REPORT" \
     --halt2-evidence "$BFX_HALT2_EVIDENCE_REPORT" \
     --config-artifact <reviewed-config-artifact-path> \
     --cells <reviewed-canary-cells-path>
   ```

   Require exit 0. The verifier derives outcome, permit binding, replay hash,
   and reconcile facts from durable rows and events; it does not resume
   trading, write a database row, or call Bitfinex. It rejects stale/missing
   evidence, identity mismatch, incomplete full-account snapshot coverage,
   nonzero lag, a nonzero exposure diff, insufficient reconcile evidence, and
   every open uncertainty. A nonzero exit leaves the halt effective.

   This runbook does not grant deployment authority, and the coding agent does
   not run it. A failure leaves deployment stopped; it is never a resume
   approval.

The additional `halt2_cutover.py` reserved names `convert-pending`,
`quarantine`, `verify`, and `release-report` likewise exit 2 as explicit
later-task operator commands. Their spelling is retained for compatibility;
the implemented conversion/quarantine interface above is
`verify_projection_replay.py`.

## Canary evidence report

The evidence report is redacted and contains exactly bounded facts such as
`account_id`, `environment`, `symbol`, `cell`, `strategy`, `amount_usdt`,
`permit_id`, `command_decision_id`, `attempt_id`, `outcome_kind`,
`venue_offer_id`, `outcome_at_ms`, `outcome_event_seq`, `reconcile_fences`,
`reconcile_observed_at_ms`, `projection_hash`, `venue_db_exposure_diff_usdt`,
`full_account_snapshot_complete`, and `stop_reason`.

```json
{
  "account_id": "<uuid>",
  "environment": "prod",
  "symbol": "fUST",
  "cell": "<approved-cell>",
  "strategy": "<approved-strategy>",
  "amount_usdt": "<minimal-approved-amount>",
  "permit_id": "<durable-permit-uuid>",
  "command_decision_id": "<redacted-decision-id>",
  "attempt_id": "<uuid>",
  "outcome_kind": "<acknowledged-or-unknown>",
  "venue_offer_id": "<redacted-venue-id-or-null>",
  "outcome_at_ms": 0,
  "outcome_event_seq": 0,
  "reconcile_fences": [0, 0],
  "reconcile_observed_at_ms": [0, 0],
  "projection_hash": "<sha256>",
  "venue_db_exposure_diff_usdt": "0",
  "full_account_snapshot_complete": true,
  "stop_reason": null
}
```

## Post-canary observation and final verification

Do not consider resume after the command outcome. Observe the two ordered,
fresh full-account reconcile cycles after that outcome. Their fences and
timestamps must strictly increase, the account-local projection hash must
remain valid, snapshot coverage must remain complete, and the venue-vs-DB
exposure diff must remain zero. Any deviation is Stop / abort and routes to
[rollback after venue write](rollback-after-venue-write.md).

Before any operator even considers a separately authorized resume, record all
of the following as passing evidence:

- API auth denial (non-operator/cross-account access is denied without account
  enumeration) and direct signup denial (`403 signup_disabled`).
- Account-local projection hash and event-chain replay from an empty projection
  with the expected event hash.
- UNKNOWN fault matrix: accept+drop, reject+drop, timeout/reset, malformed
  response, 5xx-after-side-effect, crash-before-outcome, multiple candidates,
  and out-of-order delivery all show one attempt and no automatic retry.
- The orphan quarantine retains the venue object and blocks the affected scope.
- A fresh venue full-account diff, persistent halt effectiveness, and no
  automatic retry while any UNKNOWN or other uncertainty remains open.
- Full local quality gate: begin a new shell session at the repository root so
  each CWD transition is explicit. Stop at the first nonzero result; do not
  treat an environment prerequisite as a pass.

  ```bash
  cd backend
  uv run pytest -m "not integration" -q
  uv run pytest tests/integration/test_halt2_replay_cutover.py \
    tests/integration/test_unknown_submit_pg.py \
    tests/integration/test_orphan_quarantine_pg.py -m integration -q
  uv run alembic check
  uv run mypy src/
  uv run ruff check
  cd ..
  cd frontend
  pnpm test
  pnpm lint
  pnpm build
  ```

The implementation evidence is [Plan 2 gate tests](../../backend/tests/scripts/test_halt2_cutover.py),
[Plan 3 replay/conversion tests](../../backend/tests/integration/test_halt2_replay_cutover.py),
and [Plan 4 daemon/canary tests](../../backend/tests/modules/marketfeed/test_daemon.py).
