# Offsite DR operator runbook

This runbook provisions and measures the PostgreSQL offsite disaster-recovery
foundation. It does not authorize a production restore, a resume, or a
Bitfinex request. Keep the account/environment halt and full-account reconcile
policy in force throughout the procedure.

## Acceptance boundary

- Operational RPO must be measured at **<=300 seconds**.
- An isolated restore drill must be measured at **<=3600 seconds**.
- The monthly and change-triggered restore tests use the same isolated-restore
  RTO gate of **<=3600 seconds**, aligned with the existing operational target.
- Only bounded, fresh reports with `measured: true` count as evidence. A green
  offline test, valid config, timer state, or backup object is not evidence of
  R2 reachability or recoverability.
- PostgreSQL must run with `archive_mode=on`, `archive_timeout=60s`, and
  `archive_command=pgbackrest --stanza=bfx archive-push %p`.
- Every container-side pgBackRest or `psql` data-plane command uses
  `docker exec --user postgres`.
- The restore drill uses generated resources only. It never restores or removes
  production `bfx_pgdata`, never joins a production network, and makes no Bitfinex request.

Stop on every nonzero exit, malformed result, stale report, ledger mismatch,
or cleanup failure. Timers remain disabled until every real-R2 and isolated
restore gate below has passed.

## One-time installation and measured acceptance

Start the following steps from the repository root on the VM, in order. Section
0 temporarily changes into the Terraform module directory and returns to the
repository root before Section 1.

### 0. Provision the R2 backup bucket with Terraform

The operator, not the coding agent, supplies `CLOUDFLARE_API_TOKEN` as the
Cloudflare management credential. Terraform state bucket is separate from the
pgBackRest backup bucket: provision and secure the independent state bucket
before this step. The bucket-scoped R2 S3 runtime credential used by pgBackRest
is different from `CLOUDFLARE_API_TOKEN`; the runtime R2 token is not a
Terraform resource and must never enter Terraform variables, state, plans, or
Git.

Copy both tracked examples outside Git, replace only their non-secret
placeholders, and keep the resulting files at the operator-controlled secure
paths below. The independent state bucket and its state-bucket R2 S3 runtime
credentials must already exist; provide those backend credentials through the
operator environment, never by putting them in the copied `backend.hcl`.

```bash
cp infra/terraform/r2/backend.hcl.example /secure/path/backend.hcl
cp infra/terraform/r2/terraform.tfvars.example /secure/path/terraform.tfvars
cd infra/terraform/r2
read -r -s -p 'Cloudflare management token (hidden; not saved to history): ' CLOUDFLARE_API_TOKEN
printf '\n'
export CLOUDFLARE_API_TOKEN
terraform init -backend-config=/secure/path/backend.hcl
```

If the backup bucket already exists, import it from the same module directory
after `terraform init` and before `terraform plan`, using its actual
jurisdiction. Skip this step for a newly created bucket:

```bash
terraform import -var-file=/secure/path/terraform.tfvars \
  cloudflare_r2_bucket.backup '<account_id>/<bucket_name>/<jurisdiction>'
```

Review the generated plan before applying it; apply only that reviewed plan.
Continue from `infra/terraform/r2` to generate the bootstrap plan:

```bash
terraform plan -var-file=/secure/path/terraform.tfvars -out=/secure/path/r2.tfplan
```

Stop for manual review of the rendered saved plan before applying it. After
that review, apply only the reviewed bootstrap plan and return to the
repository root:

```bash
terraform show /secure/path/r2.tfplan
terraform apply /secure/path/r2.tfplan
cd ../../..
```

The complete example and state/backend boundary are also documented in
[`infra/terraform/r2/README.md`](../../infra/terraform/r2/README.md). Terraform
manages only the backup bucket and the lifecycle rule that aborts incomplete
multipart uploads. R2 lifecycle does not own pgBackRest retention; pgBackRest
owns retention. The coding agent does not perform production terraform apply or
token creation, inject VM secrets, activate timers, run backup/restore, or call
Bitfinex.

### 1. Install the timer definitions without enabling them

Install the tracked definitions and reload systemd with the checked installer:

```bash
./scripts/install-pgbackrest-timers.sh
```

The installer copies definitions, runs `daemon-reload`, and verifies both timers
are inactive and disabled. It installs definitions only: it never enables,
starts, or stops a timer. This is not backup acceptance and must not be
followed by early timer activation.

### 2. Create and validate the VM secret fragment after provisioning R2

Use this same absolute secret directory for the wizard, host validation, and
the container mount in Section 3:

```bash
PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"
export PGBACKREST_SECRET_DIR
```

Use the VM wizard as the preferred path:

```bash
./scripts/setup-pgbackrest-r2.sh
```

The wizard creates the VM-only fragment, directs the operator to create a
bucket-scoped **Object Read & Write** R2 S3 credential, validates it, and then
installs timer definitions without activating them. Follow the official
[Cloudflare R2 token documentation](https://developers.cloudflare.com/r2/api/tokens/).
The Cloudflare management token used by Terraform is distinct from these
bucket-scoped R2 S3 runtime credentials. Keep the repository cipher passphrase
in separate offline escrow.

If the wizard is unavailable, use the following manual fallback contract. It
must produce the same private R2 Standard bucket and VM-only fragment; do not
use an account-admin token.

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
or placeholder assignment. Every direct file must be regular with a case-sensitive
`.conf` suffix and exactly one `[global]` section; assignments before that
section, wrong/extra/repeated sections and ambiguous parser syntax are rejected.

The deployed cluster's existing SQL admin role is `bfx`, distinct from OS user
`postgres` (UID/GID 70). The stanza sets `pg1-user=bfx`; status, DR health,
recovery, bootstrap and the ledger reads (restored copy and production) use SQL role `bfx`.
Container execs retain `--user postgres`. Verify that the existing role/database
match this contract before a drill; DR Compose never initializes `POSTGRES_*`.

R2 does not supply the S3 Object Lock behavior assumed by some S3 clients.
Treat R2 Bucket Lock as a separate retention control and leave it disabled
until the disposable `expire` acceptance below succeeds.

### 3. Build and validate bfx-postgres:local

Build the pinned custom image without starting production:

```bash
docker compose -f docker-compose.bot.yml build postgres
docker run --rm --user 70:70 --network none --entrypoint sh \
  --mount "type=bind,src=$PGBACKREST_SECRET_DIR,dst=/secrets,readonly" \
  --mount "type=bind,src=$(pwd)/deploy/vm/pgbackrest/pgbackrest.conf,dst=/etc/pgbackrest/pgbackrest.conf,readonly" \
  bfx-postgres:local -ec 'test "$(id -u)" = 70; test "$(id -g)" = 70; test -r /etc/pgbackrest/pgbackrest.conf; test -r /secrets; test -x /secrets; test -r /secrets/r2.conf'
```

Require the host validator and this network-free container access check to
exit zero without printing values. The latter tests the mounted permissions
as the actual pinned container identity before any R2 command. Repeat both
checks after secret replacement or ownership changes; every additional direct
file must meet the same regular, non-symlink and five-option aggregate boundary.

The tracked, non-secret `pgbackrest.conf` must also be readable by container
UID/GID 70 (for example, mode `0644`). Do not apply that public file mode to
the secret fragment. A host checkout created under umask `077` can pass Git's
clean-content check while its `0600` config prevents WAL recovery.

The restore entrypoint requires a mount at `/var/lib/postgresql` and fixes
PGDATA to `/var/lib/postgresql/18/docker`. It rejects symlink components,
nonempty PGDATA (including hidden files), and unrelated entries anywhere in
that volume. It never clears an existing directory. Use a fresh generated
volume after a failed attempt; do not restart the restore entrypoint on a
partially restored volume. It prepares both the versioned parent and PGDATA
for UID/GID 70 and checks mounted configuration access before contacting R2.
The restore command itself also runs as `postgres`: running it as root can
leave a root-owned lock directory that prevents the subsequent PostgreSQL
recovery process from fetching WAL in the same container.

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

The wrapper executes `stanza-create`, `check`, `--type=full backup`,
`--type=diff backup`, `info --output=json`, and `verify` as container OS user
`postgres`. All stdout/stderr is captured in a private, trap-cleaned temporary
directory. Only fixed stage/error markers are printed, including `smoke_info_ok`.
Never print or tee raw info JSON: even an exit-zero command can contain
endpoint/bucket diagnostics and `status.code=99`. Require exactly stanza
`bfx`, repository key 1 with the approved cipher, and strict integer
`status.code == 0` for both stanza and repository. Backup timestamps in the
pinned 2.59.1 JSON are integer epoch `timestamp.start/stop`, never nested
epoch objects; bool/float/negative/reversed values fail validation.

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
as zero lag. Refresh first invalidates old evidence with
`measured: false` / `backup_refresh_incomplete`; any failed collection stays
unmeasured. If atomic persistence fails (including ENOSPC), remove the old
artifact or truncate it if unlink is denied. `bfx-backup-check` treats that
missing/empty artifact as a problem. Wrappers never print old output after persistence failure.

### 6. Select the backup and recovery target

Pick the backup set to accept (`pgbackrest --stanza=bfx info` in step 4 lists the
labels) and, if the acceptance is for a point in time, an exact UTC recovery target
covered by that set's WAL. Omitting `--target-time` does not set a recovery cutoff;
pgBackRest replays through the available archive stream (`--type=default`, without
`--target-action`). An explicit target uses `--type=time --target=... --target-action=promote`.
Do not use `--type=immediate`: that changes the recovery boundary.

No baseline file is captured and writers need not stop. Production's own append-only
ledger, bounded by what the restored copy holds, is the baseline: the drill reads the
boundary from the restored copy, then compares production's rows within it (the same
verification as the monthly restore test below). A target in the past is therefore
compared with production's rows up to that point, whatever production wrote since. The
restored copy must be at the deployed release's schema: a backup taken before its
migrations (a legacy-schema-compatible or older-head copy) fails the boot check's
schema-head gate. Production must be reachable; a restore whose production is gone is
an incident restore, not this drill.

### 7. Run the isolated acceptance drill

Invoke the drill with the approved command shape:

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --backup-label <label>
```

Add `--target-time <YYYY-MM-DDTHH:MM:SSZ>` for a PITR target, and `--output <absolute path>`
to write the receipt elsewhere than `$HOME/bfx/dr-evidence/restore.json`. There are no
scope arguments: every scope of the restored copy is verified. The runner rejects a
malformed label, target or output before creating DR resources.

`restore-data` is the Compose logical volume key; `DR_VOLUME_NAME` supplies a
generated external Docker volume. Neither name may be production
`bfx_pgdata`. The runner creates one generated internal network and one
generated egress network. restore-db alone has temporary R2 egress during
restore/recovery. `pg_isready` only proves connections are accepted. Before
disconnect, the runner polls `SELECT pg_is_in_recovery();` on the existing
restored database as SQL role `bfx` and requires `f` within the same global
deadline. True retries; invalid output, command failure or timeout fails closed.
Only then disconnect egress, prove membership is exactly the generated internal network,
bootstrap, and start the verifier.

After restore-db has completed recovery and egress is disconnected, the runner uses the local PostgreSQL socket as OS user `postgres`
via `docker exec --user postgres --interactive`, validates the
existing restored database, and creates an ephemeral verifier role on that
database. It does not rely on `POSTGRES_USER`, `POSTGRES_PASSWORD`, or
`POSTGRES_DB` to initialize restored non-empty PGDATA and does not create a new
application database. The role password travels only through stdin and the
temporary mode-`0600` connection file. Every actual Compose subprocess receives
a sanitized environment: ambient `DR_*`, `DATABASE_URL`, `POSTGRES_*`,
`BFX_*` and `COMPOSE_*` cannot override generated interpolation or the verifier
URL. PATH and Docker transport settings such as DOCKER_HOST remain available.
The SQL admin role is never passed to the verifier as its login.

The verifier container has a validated generated name and cleanup eligibility
is recorded before the Compose run attempt. Cleanup independently force-removes
that verifier container before removing networks, even after client timeout;
normal `--rm` removal is tolerated only after confirming absence. All cleanup
shares the independent 30-second deadline; failures invalidate success.

The verifier remains on the generated internal network only. The isolated
verifier has no R2 or application secrets, no production env file, no application port,
and no Bitfinex credential. It receives only the ephemeral least-privilege
database URL (`SELECT` on the ledger tables and the boot check's extras,
`default_transaction_read_only`) and runs the image's read-only boot check; the ledger
rows are compared by the runner's bounded `COPY` reads of the restored copy and of
production (see the monthly restore test below). Any command, network, ledger, boot,
image, deadline, or cleanup failure exits nonzero and writes `measured: false`.

The runner also writes a non-authoritative stage diagnostic beside the receipt:
`restore-timing.json` for the default `restore.json` output. It contains only
`schema_version`, `kind=restore_timing`, the generated `restore_run_id`,
`complete`, and an allowlisted `stages` map of monotonic durations in seconds.
Require its non-null `restore_run_id` to equal the accepted receipt before
using the durations; this prevents a stale diagnostic from being associated
with another run. The fixed stages are `resource_setup`,
`physical_and_wal_recovery`, `isolation_bootstrap`, `verification`, and
`cleanup`. Physical restore and WAL recovery remain one stage because the
runner has no finer pgBackRest stage evidence; do not split them by subtracting
estimated delays.

`complete: true` means all five timing stages closed and the existing restore
receipt path completed successfully. A failed lifecycle may retain only its
closed stages with `complete: false`. Cleanup keeps its independent 30-second
deadline and remains outside the accepted RTO budget. Timing persistence occurs
after cleanup and cannot change, replace, or promote the accepted restore
receipt; a persistence failure removes the diagnostic when possible. Stage
durations are for bottleneck diagnosis only: they can include boundary overhead
and need not sum exactly to `rto_seconds`. Never use the timing diagnostic to
satisfy RPO/RTO, authorize a restore, a deploy, or a resume, and never add commands,
errors, database URLs, credentials, or arbitrary labels to it.

### 8. Accept only fresh measured evidence

Review `$HOME/bfx/dr-evidence/backup.json` and `restore.json` only after the
drill has finished cleanup. Both reports must be bounded, `measured: true`, and
no older than 900 seconds by strict integer `observed_at_ms`. Require
`rpo_seconds <= 300`, operational `rto_seconds <= 3600`, `kind: restore_ledger` with
`restore_test: false`, the selected `target_backup_label`/`target_time`, the
`ledger` comparison (rows compared, per-table digests, no mismatch) and the `boot`
result for every scope, immutable local image ID and pinned
labels, `network_internal: true`, `egress_disconnected: true`, and verifier
exit status zero.

Retain both reports and their digests in the release or incident evidence
bundle. A future/stale timestamp, missing field, cleanup error, or
`measured: false` report blocks acceptance. These artifacts never authorize a
resume; resuming is an operator action in the UI. Never edit
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

## Monthly and change-triggered ledger restore test

The acceptance drill above (steps 6-8) runs this same verification at an operator-chosen
backup and target. The recurring check is the restore test, which needs no writer pause,
no operator baseline and no scope configuration. Its caller names no verification mode;
the drill of the release under test picks it (today: ledger mode):

```bash
deploy/vm/pgbackrest/restore-drill.sh --restore-test
```

It restores the newest backup set (read from the production stanza with
`pgbackrest info`) to the end of the archive into the same generated, isolated
resources, then verifies the restored copy two ways:

1. **Ledger rows against production.** A read-only `psql` on the restored copy
   (`deploy/vm/pgbackrest/ledger_digest.py`) takes the boundary W of every scope from
   the restored copy itself: the newest `query_revision`, the newest observation and
   accepted observation, the newest `attempt_seq` and `opened_revision`, the attempts
   still without an outcome, and the newest `epoch_seq`. The same generated
   `COPY` script then runs on the restored copy and on production (`docker exec ... psql`
   as the admin role on the container's own socket, one `REPEATABLE READ READ ONLY`
   transaction), and each append-only ledger table's rows within W must be identical
   (count plus an order-free sha256 multiset digest of the COPY text). The bounds are
   commit-ordered per scope: every ledger writer holds the scope's advisory lock until
   commit, the tables are append-only by trigger, and each table is bounded by its own
   lock-allocated key or by the observation it was accepted with (resolutions: only
   those naming an older accepted observation than the restored latest one; outcomes:
   only for attempts that already had one). Production's later rows are outside W and
   never compared. The clock and the venue mirrors are updated in place: they are only
   checked on the restored copy (clock not behind its openings and accepts, every mirror
   row names an accepted observation of its scope) and the restored clock must not be
   ahead of production's. The restored copy's bounded rows must equal its whole tables,
   so a wrong bound fails (`ledger_bound_invalid`) instead of hiding rows. A difference is
   `ledger_digest_mismatch`; the journal names the tables (never row data). Migration
   `a6c7e8f9b0d1` rewrote append-only rows in place (JSON `null` to SQL NULL), so a drill
   whose `target_backup_label`/`target_time` stops before it reports
   `ledger_digest_mismatch` by design; replaying past it, or any later backup, matches.
2. **Read-only boot check.** The image's own entry,
   `python -m bfx_funding_bot.apps.restore_boot_check`, runs in the `bfx-bot:local` image
   (the deployed one) on the internal network, as a per-run LOGIN that
   has `SELECT` on exactly the ledger tables plus `alembic_version`, `database_realm` and the capital policy tables, and `default_transaction_read_only`: schema at the image's
   migration head, the stamped realm, epoch `ledger` from a known writer (the Bitfinex epoch guard), and the
   ledger capital reader for every (symbol, cell) of each scope's newest accepted basis,
   which must fold a basis (or report that the newest query was still pending at the
   restore point). It contacts no venue.
3. **Migration rehearsal** (the restore test only, not the acceptance drill; ADR
   `2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them`). The candidate is
   the backend image whose `org.opencontainers.image.revision` label is the drill
   checkout's own `HEAD` (repository `ghcr.io/will413028/bfx-funding-bot-backend`; bfx-deploy
   pulled it by digest before the test, and keeps the deployed one). After steps 1–2 pass,
   on a budget of its own (1200 s, of which `alembic upgrade head` gets 900 s like
   production's; the RTO is already settled), the drill sets a per-run password for the
   owner role `bfx` inside the isolated copy only (admin on the copy's socket, logging off),
   runs the candidate's `alembic upgrade head` as that owner -- the role production's
   `migrate.env` migrates with -- on the internal network with the production one-shot's
   hardening, grants the verifier the tables the migration created, re-reads the copy's
   bounds and runs the candidate's boot check on the migrated copy. Any failure here is
   `migration_rehearsal_failed` (the journal names the bounded step:
   `migration_failed`, `candidate_image_unavailable`, a boot code). Monthly, the candidate
   is the deployed image itself and nothing migrates. The success receipt carries
   `rehearsal` (revision, candidate digest, heads before and after, seconds); the failure
   receipt carries `source_revision`, the rehearsed revision, because the monthly run and a
   deploy's run write the same receipt path and bfx-deploy acts only on its own target's.
   Before the test bfx-deploy removes local images other than the target's and the last
   successful release's, so the label lookup is unambiguous.

The receipt is `$HOME/bfx/dr-evidence/restore-ledger.json` (or `--output`;
`kind: restore_ledger`, `restore_test: true`, with the per-scope bounds, per-table
digests and the boot result); it never replaces the acceptance drill's `restore.json`
(`restore_test: false`). The wrapper accepts a
fresh `measured: true` receipt with `restore_test: true`, whatever the mode. Production is only read, after the restored copy is isolated.

`bfx-restore-test@<release>.service` runs this. The monthly timer
(`bfx-restore-test.timer`, 1st of the month 09:17 UTC) starts
`bfx-restore-test@current`: the DR scripts of the deployed release, from the clean
checkout `/home/ubuntu/bfx-releases/current`. bfx-deploy starts
`bfx-restore-test@<target revision>` -- the scripts the release is about to ship --
whenever it is about to apply a migration or
ship a change under `deploy/vm/pgbackrest/`, `deploy/vm/postgres/`,
`docker-compose.bot.yml` or `docker-compose.dr.yml` (or when the diff cannot be
read); a failure alerts and blocks that deploy (a failed migration rehearsal before a
migration starts the stopped bot again, [deploy runbook](deploy.md) §5). The test needs no config file of
its own. `/home/ubuntu/bfx/restore-test.json` (the scope file of the retired prefix test)
and `$HOME/bfx/dr-evidence/restore-prefix.json` are no longer read by anything since S1-8
PR-D; the operator may delete them.
bfx-deploy creates those checkouts (git worktrees of the mirror, owned by
`ubuntu`) and points `current` at each release it deploys, so a DR change is
tested with its own scripts before it ships and runs on schedule after it
deploys. To re-run the test by hand, for example after changing the secrets:

```bash
sudo systemctl start --no-block bfx-restore-test@current.service
journalctl -fu bfx-restore-test@current.service
```

On success the service writes `$HOME/bfx/dr-evidence/restore-heartbeat.json`;
`bfx-backup-check` alerts when that heartbeat is missing or older than 35 days
(only while `bfx-restore-test.timer` is enabled). A failure alerts through
`bfx-alert@` and never touches trading state; only a deploy that asked for the
test is stopped. Enable the timer after the first successful manual run:

```bash
sudo systemctl enable bfx-restore-test.timer
sudo systemctl start bfx-restore-test.timer
```

The wrapper and the unit call only `--restore-test --output ...`, and every drill since S1-8
D3 accepts that, so a deploy (target drill, installed wrapper) and a revert to any release
since D3 need no bridge; a revert to a release from before D3 is not supported (its drill
has no `--restore-test`; the transitional `--prefix` paths were removed in S1-8 PR-D). The
drill runs the deployed image's own boot check entry, which every image since S1-8 PR-C has,
and reads its JSON by required keys (keys a newer image adds are ignored). The unit passes
only `--drill`; the receipt and heartbeat paths are the wrapper's defaults, so a tooling
install that stops between the wrapper and the unit leaves a pair that still works (an older
unit's `--legacy-*` arguments are accepted and ignored).

Each production and restored-copy read is bounded on the server
(`statement_timeout` and `transaction_timeout` at the drill's remaining budget,
`idle_in_transaction_session_timeout` 60 s, `lock_timeout` 10 s), and the receipt's
`ledger.read_seconds` (also in the heartbeat) shows how long each took.

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
[Rollback after a venue write](rollback-after-venue-write.md).
