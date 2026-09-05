# Offsite DR operator runbook

This runbook provisions and measures the PostgreSQL offsite disaster-recovery
foundation. It does not authorize a production restore, a resume, or a
Bitfinex request. Keep the account/environment halt and full-account reconcile
policy in force throughout the procedure.

## Acceptance boundary

- Operational RPO must be measured at **<=300 seconds**.
- An isolated restore drill must be measured at **<=3600 seconds**.
- The Halt 2 canary keeps its stricter isolated-restore RTO gate of **<=60
  seconds**. Meeting the operational target does not satisfy that gate.
- Only bounded, fresh reports with `measured: true` count as evidence. A green
  offline test, valid config, timer state, or backup object is not evidence of
  R2 reachability or recoverability.
- PostgreSQL must run with `archive_mode=on`, `archive_timeout=60s`, and
  `archive_command=pgbackrest --stanza=bfx archive-push %p`.
- Every container-side pgBackRest or `psql` data-plane command uses
  `docker exec --user postgres`.
- The restore drill uses generated resources only. It never restores or removes
  production `bfx_pgdata`, never joins a production network, and makes no Bitfinex request.

Stop on every nonzero exit, malformed result, stale report, baseline mismatch,
or cleanup failure. Timers remain disabled until every real-R2 and isolated
restore gate below has passed.

## One-time installation and measured acceptance

Run the following steps from the repository root on the VM, in order.

### 1. Install the four systemd unit files without enabling them

Install all four tracked unit files with mode `0644`, then reload systemd. Do
not enable or start either timer yet.

```bash
sudo install -m 0644 deploy/vm/systemd/bfx-pgbackrest-backup.service /etc/systemd/system/bfx-pgbackrest-backup.service
sudo install -m 0644 deploy/vm/systemd/bfx-pgbackrest-backup.timer /etc/systemd/system/bfx-pgbackrest-backup.timer
sudo install -m 0644 deploy/vm/systemd/bfx-pgbackrest-status.service /etc/systemd/system/bfx-pgbackrest-status.service
sudo install -m 0644 deploy/vm/systemd/bfx-pgbackrest-status.timer /etc/systemd/system/bfx-pgbackrest-status.timer
sudo systemctl daemon-reload
```

Unit installation and `daemon-reload` only make definitions available. They
are not backup acceptance and must not be followed by early timer enablement.

### 2. Create and validate the VM secret fragment after provisioning R2

Create a private Cloudflare R2 Standard bucket dedicated to this repository,
with pgBackRest objects under `/pgbackrest`. Create a bucket-scoped **Object
Read & Write** token; do not use an account-admin token. Keep the repository
cipher passphrase in separate offline escrow.

Create `$HOME/bfx/pgbackrest/conf.d/r2.conf` outside the repository. Under one
`[global]` section, the fragment contains exactly these five options, each
exactly once with an operator-supplied non-empty value:

- `repo1-s3-endpoint`
- `repo1-s3-bucket`
- `repo1-s3-key`
- `repo1-s3-key-secret`
- `repo1-cipher-pass`

This list defines the shape only; do not paste values into documentation,
shell history, tracked files, CI variables, image layers, logs, or evidence.
The secret directory and file must be non-symlinks, owned by the pinned
container PostgreSQL identity (UID/GID 70), and inaccessible to other users:
directory mode `0700`, regular file mode `0600`. Group-readable alternatives
are accepted only when the group is PostgreSQL GID 70 and no group/other write
or other-user read bit is present.

Validate the boundary without printing values:

```bash
PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"
python3 deploy/vm/pgbackrest/secret_validation.py --secret-dir "$PGBACKREST_SECRET_DIR"
```

The validator must exit zero and print nothing. `secret_config_invalid` is a
hard stop. The directory and every direct file must remain non-symlink,
readable/traversable by UID/GID 70, and contain no unknown, duplicate, empty,
or placeholder assignment.

R2 does not supply the S3 Object Lock behavior assumed by some S3 clients.
Treat R2 Bucket Lock as a separate retention control and leave it disabled
until the disposable `expire` acceptance below succeeds.

### 3. Build and validate bfx-postgres:local

Build the pinned custom image without starting production:

```bash
docker compose -f docker-compose.bot.yml build postgres
```

Inspect `bfx-postgres:local` and require the pinned PostgreSQL base digest,
pgBackRest version, and pgBackRest source SHA-256 labels. During the separately
authorized maintenance rollout, confirm the existing PostgreSQL cluster is
healthy and its effective command contains `archive_mode=on`,
`archive_timeout=60s`, and the fixed archive command before contacting R2.
Image build or offline inspection alone is not production acceptance.

### 4. Exercise the real R2 lifecycle

Keep timers disabled. Against the intended private R2 repository, run the
operator-confirmed smoke wrapper:

```bash
deploy/vm/pgbackrest/smoke.sh --confirm-r2-smoke
```

The wrapper executes this exact fail-fast sequence as container user
`postgres`; review each stage independently rather than treating a later
command as proof that an earlier result was valid:

```bash
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx stanza-create
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx check
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx --type=full backup
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx --type=diff backup
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx info --output=json
docker exec --user postgres bfx-postgres pgbackrest --stanza=bfx verify
```

Require real R2 success for `stanza-create`, archive `check`, full backup,
differential backup, `info`, and `verify`. Authentication, list/head/read,
write/multipart, or archive failure is a hard stop.

Test deletion compatibility only against a dedicated disposable R2 repository,
never the production repository. Point a separately controlled disposable
test container at that repository, create disposable backups with the same
smoke sequence, and then exercise retention deletion:

```bash
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx expire
```

Record that the dedicated disposable R2 repository accepted the lifecycle.
Do not run this `expire` acceptance against production objects and do not call
the production repository WORM. After the disposable test, restore and
revalidate the intended production five-option secret fragment before
continuing.

### 5. Produce bounded backup evidence

Run explicit wrapper backups and then the archive/status preflight:

```bash
deploy/vm/pgbackrest/backup.sh --type full
deploy/vm/pgbackrest/backup.sh --type diff
deploy/vm/pgbackrest/preflight.sh --output "$HOME/bfx/dr-evidence/backup.json"
```

Require exit zero and a bounded `backup.json` with `measured: true`,
`rpo_seconds <= 300`, fresh `observed_at_ms`, stanza `bfx`, repository `r2`, a
bounded last archived WAL name, latest backup label, and tracked config digest.
Unavailable, stale, malformed, or failed archive state must not be interpreted
as zero lag. Any failed collection replaces old green evidence with
`measured: false`.

### 6. Capture the same-target bounded baseline.json

While writes remain halted, select the exact backup label and optional PITR
timestamp for the drill. Capture `baseline.json` from the same backup/PITR target
state: the database must not advance between the target and baseline
capture. The artifact is an absolute-path, non-symlink regular JSON file of at
most 64 KiB with exactly the following fields and no extras:

| Field | Bounded contract |
|---|---|
| `target_backup_label` | Selected pgBackRest label, matching the drill request byte-for-byte. |
| `target_time` | JSON null or the selected UTC `YYYY-MM-DDTHH:MM:SSZ` PITR time, matching the request. |
| `database_name` | Existing restored database; PostgreSQL identifier of at most 63 characters. |
| `account_id` | Canonical lowercase UUID selected for replay. |
| `environment` | Exact drill realm: `prod`, `shadow`, or `ci`. |
| `projector_version` | Exact bounded projector identifier used by the verifier. |
| `migration_heads` | One to 32 unique migration identifiers, each at most 128 characters. |
| `event_count` | Strict non-negative JSON integer; booleans and floats are invalid. |
| `event_head` | Strict non-negative JSON integer, or null only when `event_count` is zero. |
| `event_hash` | Lowercase 64-hex SHA-256 of the selected account/environment event chain. |

Generate the count, head, hash, and migration heads through the approved
read-only evidence path; do not hand-edit them. Missing or mismatched baseline
fails the drill. A baseline for a different label, target time, account,
environment, projector, or database state cannot be reused.

### 7. Run the staged isolated restore with --baseline

Invoke the drill with the approved command shape:

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --account-id <canonical-uuid> \
  --environment prod \
  --projector-version projector-v3 \
  --backup-label <label> \
  --baseline /absolute/path/baseline.json
```

If a PITR time is selected, add `--target-time` and require it to match
`target_time` in the same baseline. The runner rejects a relative, missing,
oversized, malformed, or mismatched artifact before creating DR resources.

`restore-data` is the Compose logical volume key; `DR_VOLUME_NAME` supplies a
generated external Docker volume. Neither name may be production
`bfx_pgdata`. The runner creates one generated internal network and one
generated egress network. restore-db alone has temporary R2 egress during
restore, and the runner disconnects that egress before verifier starts.

After restore-db is healthy, the runner uses the local PostgreSQL socket as OS user `postgres`
via `docker exec --user postgres --interactive`, validates the
existing restored database, and creates an ephemeral verifier role on that
database. It does not rely on `POSTGRES_USER`, `POSTGRES_PASSWORD`, or
`POSTGRES_DB` to initialize restored non-empty PGDATA and does not create a new
application database. The role password travels only through stdin and the
temporary mode-`0600` connection file.

The verifier remains on the generated internal network only. The isolated
verifier has no R2 or application secrets, no production env file, no application port,
and no Bitfinex credential. It receives only the ephemeral least-privilege
database URL and bounded replay identity, then checks migration heads, event
count/head/hash, and all fixed empty-projector counts/hashes against the
same-target baseline. Any command, network, baseline, replay, image, deadline,
or cleanup failure exits nonzero and writes `measured: false`.

### 8. Accept only fresh measured evidence

Review `$HOME/bfx/dr-evidence/backup.json` and `restore.json` only after the
drill has finished cleanup. Both reports must be bounded, `measured: true`, and
no older than 900 seconds by strict integer `observed_at_ms`. Require
`rpo_seconds <= 300`, operational `rto_seconds <= 3600`, the selected target
label/time, the baseline event identity, immutable local image ID and pinned
labels, `network_internal: true`, `egress_disconnected: true`, and verifier
exit status zero.

Retain both reports and their digests in the release or incident evidence
bundle. A future/stale timestamp, missing field, cleanup error, or
`measured: false` report blocks acceptance. Halt 2 may consume the same fresh
artifacts but remains blocked unless `rto_seconds <= 60` and all its other
event, projection, reconcile, image, and configuration gates pass. Never edit
JSON to manufacture acceptance.

### 9. Enable, start, and list the timers

Only after the real R2 lifecycle, disposable `expire`, same-target isolated
restore, cleanup, and fresh measured evidence all pass may the operator enable
and start the timers:

```bash
sudo systemctl enable bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer
sudo systemctl start bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer
systemctl list-timers --all bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer
```

Confirm both timers are loaded and scheduled. The backup timer runs daily at
03:17 UTC and selects full on Sunday, differential otherwise; the status timer
runs every five minutes. Neither timer restores, resumes, deletes production
data, changes halt state, or calls Bitfinex.

### 10. Declare DR ready only from measured acceptance

Declare this foundation DR-ready only after the enabled timers are visible and
the fresh backup/restore evidence bundle satisfies every gate above. Offline
tests, image build, systemd syntax, or timer scheduling alone do not establish
R2 reachability, restore success, production rollout acceptance, or RPO/RTO.

## Rotation and incident posture

R2 token rotation and repository cipher rotation are separate procedures. For
token rotation, keep the system halted, update only the VM secret fragment via
the approved secret channel, rerun the shared validator, archive check, backup
preflight, and isolated restore, then revoke the old token only after fresh
evidence passes. For cipher rotation, plan a separate repository migration and
retain every old passphrase needed to read existing backups.

A production database restore cannot undo a venue write. If a Bitfinex write
may have occurred after a candidate restore point, do not restore production;
retain halt, perform a fresh full-account reconcile, and use the
adopt/manual-resolution/forward-fix process in
[Rollback after a Halt 2 venue write](rollback-after-venue-write.md).
