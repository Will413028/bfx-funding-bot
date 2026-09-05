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
Use a host-operator-owned directory and regular file, both with PostgreSQL
GID 70: directory mode `0750`, file mode `0640`. The host operator (the same
login that runs deploy, validation, and restore) reads as owner; container
PostgreSQL UID/GID 70 reads/traverses through the group. No group write or
other-user permissions are allowed. Do not use container-owned `0700/0600`
for this host-operated workflow. Use ordinary Linux bind mounts without UID
remapping; if user namespaces or ACLs change access, stop and resolve that
mapping before continuing.

Validate the boundary without printing values:

```bash
PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"
test -d "$PGBACKREST_SECRET_DIR" && test ! -L "$PGBACKREST_SECRET_DIR"
test -f "$PGBACKREST_SECRET_DIR/r2.conf" && test ! -L "$PGBACKREST_SECRET_DIR/r2.conf"
sudo chown "$(id -u):70" "$PGBACKREST_SECRET_DIR" "$PGBACKREST_SECRET_DIR/r2.conf"
sudo chmod 0750 "$PGBACKREST_SECRET_DIR"
sudo chmod 0640 "$PGBACKREST_SECRET_DIR/r2.conf"
test -r "$PGBACKREST_SECRET_DIR" && test -x "$PGBACKREST_SECRET_DIR"
test -r "$PGBACKREST_SECRET_DIR/r2.conf"
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
docker run --rm --user 70:70 --network none --entrypoint sh \
  --mount "type=bind,src=$PGBACKREST_SECRET_DIR,dst=/secrets,readonly" \
  bfx-postgres:local -ec 'test "$(id -u)" = 70; test "$(id -g)" = 70; test -r /secrets; test -x /secrets; test -r /secrets/r2.conf'
```

Require the host validator and this network-free container access check to
exit zero without printing values. The latter tests the mounted permissions
as the actual pinned container identity before any R2 command. Repeat both
checks after secret replacement or ownership changes; every additional direct
file must meet the same regular, non-symlink and five-option aggregate boundary.

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
never the production repository. Provision a separate disposable cluster,
container, empty bucket, bucket-scoped token, cipher, secret mount, PGDATA and
spool; none may refer to production resources. Inspect its mounts and review
the bucket/token scope through the approved secret channel without printing
values. Set `DISPOSABLE_CONTAINER` to that inspected container. Its name alone
does not prove repository isolation.

In a dedicated disposable copy of the non-secret pgBackRest config, retain
the pinned options and disabled raw logging, but set `repo1-retention-full=1`,
`repo1-retention-diff=1`, and `expire-auto=n`. Do not put these options in the
five-option secret fragment or change the tracked production retention
(`full=4`, `diff=6`). Disabling automatic expiration preserves both old sets
until the explicit test. Run the following only after confirming isolation:

The [pgBackRest retention reference](https://pgbackrest.org/configuration.html#section-repository/option-repo-retention-full)
defines expiration by completed backup count; the
[expire-auto option](https://pgbackrest.org/configuration.html#section-backup/option-expire-auto)
controls automatic expiration after backup.

```bash
test -n "$DISPOSABLE_CONTAINER" && test "$DISPOSABLE_CONTAINER" != bfx-postgres
umask 077
DISPOSABLE_EVIDENCE_DIR=$(mktemp -d)
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx stanza-create
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx check
for backup_type in full diff full diff; do
  docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx --type="$backup_type" backup
done
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx verify
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx info --output=json > "$DISPOSABLE_EVIDENCE_DIR/before.json"
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx expire
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx info --output=json > "$DISPOSABLE_EVIDENCE_DIR/after.json"
python3 - "$DISPOSABLE_EVIDENCE_DIR" <<'PY'
import json
import sys
from pathlib import Path

def inventory(path):
    stanzas = json.loads(path.read_text())
    assert len(stanzas) == 1 and stanzas[0]["name"] == "bfx"
    assert stanzas[0]["status"]["code"] == 0
    return {backup["label"]: backup["type"] for backup in stanzas[0]["backup"]}

directory = Path(sys.argv[1])
before = inventory(directory / "before.json")
after = set(inventory(directory / "after.json"))
full = sorted(label for label, kind in before.items() if kind == "full")
diff = sorted(label for label, kind in before.items() if kind == "diff")
assert len(full) == 2 and len(diff) == 2
assert diff[0].startswith(full[0] + "_") and diff[1].startswith(full[1] + "_")
assert after == {full[-1], diff[-1]}
assert set(before) - after == {full[0], diff[0]}
PY
docker exec --user postgres "$DISPOSABLE_CONTAINER" pgbackrest --stanza=bfx verify
```

Retain both inventories and require all assertions, `expire`, and the final
`verify` to pass. Also compare an approved R2 object-list inventory of this
disposable bucket before/after: objects under each expired
`/pgbackrest/backup/bfx/<expired-label>/` prefix must be absent, while retained-set
objects remain readable. Separate `backup.history` audit manifests may remain.
Inventory
no-op, lock-denied deletion, missing retained objects, or any nonzero command
blocks the gate; exit zero alone is insufficient. Never point `expire` at
production. Do not call the production repository WORM. Production secrets
remain in their own mount; revalidate that intended five-option fragment
before continuing.

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

The baseline is operator-supplied. The two bounded steps below use existing
`psql`, canonical event/replay functions, and the baseline loader; this is
not an automatic production baseline generator or a runner-side capture.
Do not hand-edit derived count/head/hash/migration fields. Missing or mismatched baseline
fails the drill. A baseline for a different label, target time, account,
environment, projector, or database state cannot be reused.

Before selecting a fresh backup, quiesce **all database writers**, including
bot, webapi, jobs and migrations, through the separately approved maintenance
procedure. A trading halt alone does not stop event or schema writes. Maintain
that quiescence from before the selected backup starts until capture finishes;
retain the backup `info` label/start/stop and maintenance interval in the
operator evidence bundle. For PITR, the selected UTC target must fall after
that backup's completion, within the same unchanged interval, with archived
WAL coverage confirmed. Do not approximate PITR by filtering `occurred_at_ms`:
event time is not commit time. If the target predates quiescence or any writer
advanced state, stop and select a fresh backup, or use independently approved
read-only tooling against an isolated copy restored to that exact target.
Never derive the expected baseline from the drill being accepted.
If step 5's backups predate this quiescent interval, repeat its full/diff
backups and preflight within the interval and select the new label.

First, capture only the selected account UUID and exact environment from the
existing database in one read-only snapshot. `CAPTURE_CONTAINER` identifies
the approved source cluster; `DATABASE_NAME` is its existing database, not a
new database. Enter the non-secret scope/target fields from the approved drill
request. Use `prod` only for that exact production account; never aggregate
legacy `account_id` strings, other accounts, or other environments.

```bash
umask 077
BASELINE_WORK_DIR=$(mktemp -d)
read -r -p 'Approved source container: ' CAPTURE_CONTAINER
read -r -p 'Existing database: ' DATABASE_NAME
read -r -p 'Canonical account UUID: ' ACCOUNT_ID
read -r -p 'Exact environment (prod/shadow/ci): ' DR_ENVIRONMENT
read -r -p 'Selected backup label: ' BACKUP_LABEL
read -r -p 'UTC PITR target, or empty for backup end: ' TARGET_TIME
read -r -p 'Projector version: ' PROJECTOR_VERSION
docker exec --user postgres --interactive "$CAPTURE_CONTAINER" \
  psql -X -qAt --no-password --host /var/run/postgresql --username postgres \
  --dbname "$DATABASE_NAME" --set ON_ERROR_STOP=1 \
  --set account_id="$ACCOUNT_ID" --set environment="$DR_ENVIRONMENT" \
  > "$BASELINE_WORK_DIR/capture.json" <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
WITH scoped AS (
  SELECT * FROM public.event_log
  WHERE exchange_account_id = :'account_id'::uuid
    AND deployment_environment = :'environment'
)
SELECT json_build_object(
  'database_name', current_database(),
  'account_id', :'account_id', 'environment', :'environment',
  'migration_heads', (SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version),
  'event_count', (SELECT count(*) FROM scoped),
  'event_head', (SELECT max(event_seq) FROM scoped),
  'events', COALESCE((SELECT json_agg(scoped ORDER BY event_seq) FROM scoped), '[]'::json)
);
ROLLBACK;
SQL
```

Keep the raw capture private, outside git/logs/evidence reports; it contains
account event payloads. An empty/partial capture or query failure is a hard
stop. The following offline assembly accepts at most 64 MiB of capture; larger
streams need a separately reviewed bounded export procedure. It imports the
existing canonical hash (including historical UUID derivation) and replay
identity validation instead of hashing SQL/JSON text or using a different
serialization. It opens no database connection and makes no venue request.

Run from `backend_py/` using its existing Python 3.13 environment:

The current verifier registry supports `execution-state-v1`; use that exact
value for `PROJECTOR_VERSION` and the drill request. A label such as
`projector-v3` passes identifier syntax but is not a supported implementation.

```bash
uv run python - "$BASELINE_WORK_DIR" "$DATABASE_NAME" "$ACCOUNT_ID" \
  "$DR_ENVIRONMENT" "$BACKUP_LABEL" "$TARGET_TIME" "$PROJECTOR_VERSION" <<'PY'
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path.cwd().parent / "deploy/vm/pgbackrest"))
from evidence import RestoreBaseline, load_restore_baseline
from scripts.verify_projection_replay import replay_event_log
from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

directory, database, account, environment, label, target, projector = sys.argv[1:]
source = Path(directory) / "capture.json"
assert source.is_file() and not source.is_symlink()
assert source.stat().st_size <= 64 * 1024 * 1024
capture = json.loads(source.read_text())
assert capture["database_name"] == database
assert capture["account_id"] == account == str(UUID(account))
assert capture["environment"] == environment
assert type(capture["event_count"]) is int and capture["event_count"] >= 0
assert capture["event_head"] is None or type(capture["event_head"]) is int
rows = []
for values in capture["events"]:
    values["exchange_account_id"] = UUID(values["exchange_account_id"])
    if values["event_id"] is not None:
        values["event_id"] = UUID(values["event_id"])
    rows.append(EventLogRow(**values))
report = replay_event_log(rows, account_id=UUID(account), environment=environment,
                          projector_version=projector)
assert capture["event_count"] == len(rows)
assert capture["event_head"] == report.event_head
baseline = RestoreBaseline(
    target_backup_label=label, target_time=target or None,
    database_name=database, account_id=account, environment=environment,
    projector_version=projector, migration_heads=tuple(capture["migration_heads"]),
    event_count=capture["event_count"], event_head=capture["event_head"],
    event_hash=canonical_event_hash(rows),
)
payload = json.dumps(asdict(baseline), sort_keys=True).encode()
assert len(payload) <= 64 * 1024
output = Path(directory) / "baseline.json"
assert output.is_absolute()
with output.open("xb") as handle:
    handle.write(payload)
output.chmod(0o600)
load_restore_baseline(
    output, target_backup_label=label, target_time=target or None,
    account_id=account, environment=environment, projector_version=projector,
)
PY
```

Require exit zero, record the capture/baseline digests and exact backup/PITR
association in the private operator bundle, and supply the generated absolute
`$BASELINE_WORK_DIR/baseline.json` path to the drill. Loader success checks
shape and request identity; it cannot prove the operator's quiescence/target
association. That independent evidence is mandatory. Return to the repository
root for step 7. If capture makes backup evidence older than 900 seconds,
refresh the archive/status preflight before final evidence acceptance.

### 7. Run the staged isolated restore with --baseline

Invoke the drill with the approved command shape:

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --account-id <canonical-uuid> \
  --environment prod \
  --projector-version execution-state-v1 \
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
