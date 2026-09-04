# Offsite DR operator runbook

This runbook provisions and measures the PostgreSQL offsite disaster-recovery
foundation. It does not authorize a production restore, a resume, or a
Bitfinex request.

## Scope and gates

- Operational RPO must be measured at **<=300 seconds**.
- An isolated restore drill must be measured at **<=3600 seconds**.
- The Halt 2 canary keeps its stricter isolated-restore RTO gate of **<=60
  seconds**. Meeting the operational target does not satisfy this canary gate.
- Only bounded reports with `measured: true` count as evidence. A configured
  repository, a green offline test, or an existing backup object is not
  measurement evidence.
- This foundation restores only to generated, isolated DR resources. It never
  restores the production PostgreSQL volume.

Keep the current account/environment halt and reconcile policy in force. A DR
measurement proves database recoverability and data integrity; it does not
prove venue state and does not grant permission to resume writes.

## Provision R2 and the VM secret boundary

### 1. Create the private R2 bucket

Create a **private Cloudflare R2 Standard bucket** dedicated to this
repository. pgBackRest stores objects beneath the `/pgbackrest` prefix. Do not
place an account identifier, bucket name, endpoint, or any credential in this
repository or in evidence.

This design makes no S3 Object Lock assumption because R2 does not provide the
required S3 Object Lock behavior. R2 Bucket Lock is a separate retention
control: enable it only after a disposable-bucket test proves that pgBackRest
`expire` can still perform its required deletion lifecycle. Until that test is
recorded, leave Bucket Lock disabled for this repository and do not describe
the repository as WORM.

### 2. Create a bucket-scoped Object Read & Write token

Create an R2 API token scoped only to the dedicated bucket with **Object Read
& Write** permission. pgBackRest needs bounded list, read, write, delete, and
multipart object operations inside that bucket. Never use an account-admin
token. Transfer the access key and secret through the approved secret channel;
do not put them in shell history, tracked files, image layers, logs, CI
variables, or evidence.

### 3. Create the VM secret fragment

Outside the repository, create
`$HOME/bfx/pgbackrest/conf.d/r2.conf`. The fragment supplies only the
pgBackRest options for the R2 endpoint, bucket, access key, secret key, and
repository cipher passphrase. Do not copy their values into this runbook.

The file must be non-empty, readable by the container `postgres` user through
the read-only mount, and inaccessible to other users. Keep the repository
cipher passphrase separately in offline escrow; losing it makes the encrypted
backup repository unreadable. The R2 token is not a substitute for that
escrow.

## Bootstrap in this order

Run these steps from the repository root on the VM. Stop on every nonzero exit
or malformed result. Do not enable either timer until all preceding checks and
explicit backups have succeeded.

### 4. Build and validate bfx-postgres:local

Build the pinned custom image:

```bash
docker compose -f docker-compose.bot.yml build postgres
```

Use the normal deployment procedure to start PostgreSQL, then inspect its
existing health status and the pinned image metadata. Confirm PostgreSQL is
healthy and the expected pgBackRest binary/config mounts are present before
continuing. Image validation must not contact R2 or start application writes.

### 5. Create the stanza with stanza-create

```bash
docker exec bfx-postgres pgbackrest --stanza=bfx stanza-create
```

### 6. Verify archive access with pgbackrest --stanza=bfx check

```bash
docker exec bfx-postgres pgbackrest --stanza=bfx check
```

This is an explicit archive check. An authentication, repository, or archive
failure is a hard stop.

### 7. Create an explicit full backup

```bash
deploy/vm/pgbackrest/backup.sh --type full
```

### 8. Create an explicit differential backup

```bash
deploy/vm/pgbackrest/backup.sh --type diff
```

Inspect the bounded status through the preflight wrapper. It performs one
archive check, then calls the fail-closed status collector and writes backup
evidence:

```bash
deploy/vm/pgbackrest/preflight.sh --output "$HOME/bfx/dr-evidence/backup.json"
```

Require exit 0, `measured: true`, `rpo_seconds <= 300`, stanza `bfx`, repository
`r2`, a bounded last archived WAL name, a latest backup label, and the tracked
config digest. Unavailable, stale, malformed, or failed archive state must not
be interpreted as zero lag.

### 9. Enable the backup and status timers

Only after the explicit full and differential backups and status evidence pass:

```bash
sudo systemctl enable --now bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer
```

## Scheduled operation and fail-closed response

`bfx-pgbackrest-backup.timer` runs at **03:17 UTC every day**. Its one-shot
service selects a full backup on Sunday and a differential backup on every
other day. `bfx-pgbackrest-status.timer` runs every five minutes and writes
bounded status evidence under `$HOME/bfx/dr-evidence/`, with the current backup
report at `backup.json`.

Any missing secret, R2 authentication error, pgBackRest error, malformed
output, missing complete backup set, or archive lag above 300 seconds fails
closed. Preserve the bounded evidence, keep the system halted, and investigate
the repository and archive path. Status failure does not autoheal, restart, or
restore PostgreSQL, change halt state, delete repository objects, or resume
workers.

### 10. Run an isolated restore drill

Before a release-gate or incident drill, retain the current persistent halt and
record a fresh full-account reconcile according to the applicable Halt 1/Halt
2 policy. For a routine monthly drill, the procedure remains read-only with
respect to production and cannot change halt or resume state.

Select an existing pgBackRest backup label deliberately. Invoke
`deploy/vm/pgbackrest/restore-drill.sh` with the account UUID via
`--account-id`, the realm via `--environment`, the deployed projector identity
via `--projector-version`, and the selected label via `--backup-label`. Use the
optional `--target-time` only for an operator-approved UTC ISO-8601 PITR target.
These values are operational inputs and must not be copied into tracked docs.

The drill creates only randomly named `bfx-dr-` resources on an internal-only
Docker network. It does not mount application/Bitfinex secrets, expose ports,
start the daemon or web API, or access the production volume. Require the
restored schema and account-scoped row counts, expected event head and event
hash, and every empty-projector replay count/hash to match. A schema, event,
projection, isolation, command, or cleanup failure makes the drill nonzero.

The drill writes bounded evidence to
`$HOME/bfx/dr-evidence/restore.json`. It removes only its generated container,
volume, network, and temporary environment file on success or failure; failure
evidence and its bounded redacted failure log remain for investigation. Never
substitute a production-volume restore for this drill.

### 11. Record measured RPO/RTO evidence

Retain `backup.json` and `restore.json` with their digests in the release or
incident evidence bundle. Require both reports to be current, bounded, and
`measured: true`; require `rpo_seconds <= 300` and operational
`rto_seconds <= 3600`. Halt 2 may consume the same bounded artifacts, but its
canary remains blocked unless restore `rto_seconds <= 60` and all other Halt 2
event, projection, reconcile, image, and configuration gates pass.

Do not edit JSON to manufacture a measurement, and do not treat timer state,
object presence, or test success as measured RPO/RTO evidence.

## Rotation and incidents

R2 token rotation and repository cipher rotation are separate procedures:

- For an **R2 token rotation**, keep the system halted, create a replacement
  bucket-scoped Object Read & Write token, update only the VM secret fragment
  through the secret channel, then repeat archive check and measured backup
  preflight. Revoke the old token only after the new bounded evidence passes.
- For a **cipher rotation**, plan a separate encrypted-repository migration or
  rebuild under a maintenance halt. Preserve the old passphrase in offline
  escrow for every backup encrypted with it, validate a full backup and an
  isolated restore under the new cipher, and switch only with measured
  evidence. Changing an R2 token does not rotate repository encryption.

An archive or authentication failure keeps the system halted and triggers
investigation; it never triggers automatic restore or resume. Preserve bounded
evidence and verify archive continuity, a complete backup set, and a successful
isolated restore before any separately authorized recovery decision.

A production database restore cannot undo a venue write. If a Bitfinex write
may have occurred after a candidate restore point, do not restore production;
retain halt, perform fresh full-account reconcile, and use the adopt/manual
resolution/forward-fix process in
[Rollback after a Halt 2 venue write](rollback-after-venue-write.md).
