# Offsite DR Foundation Hardening Implementation Plan

> **Historical/superseded notice (2026-09-12):** This completed implementation plan records the former Halt 2 restore RTO target of ≤60 seconds. It is not the active policy; operators must follow the current [offsite DR runbook](../../runbooks/offsite-dr.md) and [DR design spec](../specs/2026-09-04-offsite-dr-cloudflare-r2-design.md), which define the approved Halt 2 target of ≤3600 seconds. The original plan text and historical benchmark/timeout facts below are preserved.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正第一版 offsite DR foundation 的 RPO、R2 egress、restored-cluster bootstrap、baseline verification、secret boundary 與 evidence lifecycle 缺口，使 branch 可進入 final review。

**Architecture:** Production PostgreSQL 以 `archive_timeout=60s` 搭配 pinned pgBackRest image 持續 archive；所有 container-side data-plane command 以 `--user postgres` 執行，pgBackRest raw log 不落地。DR restore 使用兩個 generated external Docker network：restore-db 在還原階段暫時使用可出站至 R2 的 egress network，SQL role `bfx` 確认 recovery 完成後斷開 egress，再由只位於 internal network 的 verifier 以 ephemeral least-privilege role 對既有 database 執行 replay。

**Tech Stack:** PostgreSQL 18 Alpine, pgBackRest 2.59.1, Cloudflare R2 S3 API, Docker Compose, Bash, Python 3.13 standard library, systemd, pytest。

**Spec:** docs/superpowers/specs/2026-09-04-offsite-dr-cloudflare-r2-design.md

## Global Constraints

- PostgreSQL base image 固定為 postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2。
- pgBackRest 固定為 2.59.1 distribution tarball，build 時核對 SHA-256 1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d。
- Production PostgreSQL command 必須同時包含 `archive_mode=on`、`archive_timeout=60s` 與 `archive_command=pgbackrest --stanza=bfx archive-push %p`。
- R2 使用 S3 region auto、path URI style、private Standard bucket、/pgbackrest prefix 與 bucket-scoped Object Read & Write token。
- pgBackRest 固定 repo1-block=y、repo1-bundle=y、archive-async=y、start-fast=y、repo1-cipher-type=aes-256-cbc。
- pgBackRest tracked config 固定 `log-level-file=off`、`log-level-console=off`、`log-level-stderr=off`，不寫 persistent raw diagnostics。
- 所有 `docker exec` 執行 pgBackRest 或 psql 時必須帶 `--user postgres`；restore role bootstrap 的 password 只能經 stdin input，不得放入 argv。
- operational target 是 RPO ≤300 秒、restore drill RTO ≤3600 秒；Halt 2 canary 的 restore gate 仍是 RTO ≤60 秒。
- DR internal network 由 runner 以 `docker network create --internal` 建立；DR egress network 由 runner 以不帶 `--internal` 的 command 建立，只有 restore-db 可加入，且 verifier 啟動前必須 disconnect。
- `restore-data` 是 Compose logical volume key；其 external Docker volume name 由 `DR_VOLUME_NAME` 注入。`bfx_pgdata` 是 production-only，任何 restore command、temporary volume、temporary network 都必須拒絕使用它。
- restored non-empty PGDATA 不依賴 `POSTGRES_USER`、`POSTGRES_PASSWORD` 或 `POSTGRES_DB` 初始化；drill 必須驗證既有 database 並建立 generated ephemeral verifier role。
- 每次 measured restore 都必須有 bounded baseline artifact，且 target backup label/time、account/environment/projector、migration heads、event count、event head 與 event hash 全部一致後才可寫 `measured:true`。
- R2 endpoint、bucket、access key、secret key、repository cipher passphrase、temporary verifier password 不進 git、CI variables、shell history、Docker image layer、command diagnostics、evidence JSON 或 persistent logs。
- 所有 Bash wrapper 使用 `set -euo pipefail`；外部 command failure、malformed output、missing credential、stale archive、deadline exceeded 與 verifier failure 一律 nonzero，並以 atomic `measured:false` report 取代舊的 green report。
- restore lifecycle 的 non-cleanup commands 共用 monotonic 3600-second deadline；cleanup 使用獨立 ≤30-second deadline，只清理本次 generated resources。
- 不修改 application event schema、projection implementation、venue reconcile 或 Halt 2 state-transition/state-machine logic；Halt 2 artifact reader 可增加 DR evidence freshness/type validation。
- 先寫 offline contract tests，再寫 implementation；預設 tests 不需要 R2 credential、Docker daemon 或 production database。
- 完成時必須從 backend_py/ 執行 `uv run pytest -m "not integration"`、`uv run mypy src/`、`uv run ruff check`，另做 Compose、Bash、systemd syntax checks。

---

## File map

### Production archive and secret boundary

- Modify `deploy/vm/pgbackrest/pgbackrest.conf`：加入 archive/log boundary options。
- Modify `docker-compose.bot.yml`：加入 `archive_timeout=60s`，保留現有 service topology。
- Modify `deploy/vm/pgbackrest/status.sh`、`preflight.sh`、`backup.sh`、`smoke.sh`：user identity 與 failure evidence。
- Create `deploy/vm/pgbackrest/secret_validation.py`：共用 exact-option、permission、symlink、placeholder validator。
- Modify `scripts/deploy-vm.sh`：呼叫 shared validator，掃描 endpoint/bucket/key/cipher option。

### Restore plan and lifecycle

- Modify `docker-compose.dr.yml`：使用 `restore-data` logical volume，增加 generated egress network，移除 `POSTGRES_*` initialization contract。
- Modify `deploy/vm/pgbackrest/restore_commands.py`：建立兩個 network、ephemeral verifier role、database/baseline-safe argv 與 disconnect lifecycle。
- Modify `deploy/vm/pgbackrest/restore_drill.py`：baseline loading、local bootstrap、egress disconnect、image metadata、global deadline、fresh evidence 與 independent cleanup。
- Modify `deploy/vm/pgbackrest/evidence.py`：strict numeric parsing、baseline comparison、target/freshness/image fields、atomic failure overwrite。
- Modify `backend_py/scripts/halt2_cutover.py` only in `_read_dr_measurement()`/artifact validation helpers to reject stale or non-strict DR evidence; do not alter Halt 2 state transitions。

### Tests and documentation

- Modify `backend_py/tests/scripts/test_offsite_dr_image.py`：archive timeout/log contracts。
- Modify `backend_py/tests/scripts/test_offsite_dr_evidence.py`：strict values, baseline, image metadata, freshness and failure overwrite contracts。
- Modify `backend_py/tests/scripts/test_offsite_dr_restore.py`：two-network lifecycle, logical volume, bootstrap and baseline contracts。
- Modify `backend_py/tests/scripts/test_offsite_dr_ops.py`：user identity, shared validator, stale overwrite, deployment/runbook ordering。
- Modify `backend_py/tests/scripts/test_halt2_cutover.py`：freshness/type validation fixtures。
- Modify `docs/runbooks/offsite-dr.md`、`backend_py/ARCHITECTURE.md`、`docs/runbooks/halt-1-exchange-account-cutover.md`：document the approved boundary without personal paths or credentials。

## Stable interfaces

### Final broad-review corrections

The final fix pass adds these required contracts to all tasks below:

- SQL admin is the existing `bfx` role; stanza `pg1-user=bfx`, healthcheck,
  recovery, bootstrap, schema and baseline capture must agree. OS exec remains
  `postgres`; DR Compose has no `POSTGRES_*` initialization.
- Pinned 2.59.1 info uses strict integer epoch `timestamp.start/stop`. Reject
  bool/float/string/negative values and stop-before-start. Validate stanza
  `bfx`, repository key 1/cipher and integer zero status codes; raw fields
  never enter bounded evidence.
- All actual Compose subprocesses receive sanitized env, removing ambient
  `DR_*`, `DATABASE_URL`, `POSTGRES_*`, `BFX_*`, `COMPOSE_*` while retaining
  PATH/DOCKER_HOST. Fake runner and real subprocess interfaces accept `env`.
- `RestorePlan` includes generated `verifier_container_name` and fixed validated
  `sql_admin_role="bfx"`. Record verifier cleanup eligibility before run.
  Independently force-remove it before networks within the 30-second cleanup
  budget; after nonzero removal require a successful exact-name listing proving
  absence to tolerate normal auto-removal.
- Smoke privately captures every stage in a trap-cleaned directory and emits
  fixed markers only. `info` exit zero does not bypass status validation.
- Backup/status/preflight invalidate old evidence before capture; centralized
  atomic persistence removes/truncates stale green on ENOSPC. Wrappers cannot
  print old artifacts on nonzero persistence.
- Halt 1 Step 2 retains a legacy-schema-compatible backup/restore gate using
  realm/count/event integrity. Canonical UUID baseline + staged UUID replay
  occur only at the linked post-identity gate after additive migration and
  cutover verification (and contract/grants), before worker restart.

### Secret validator

`deploy/vm/pgbackrest/secret_validation.py` exposes:

```python
REQUIRED_OPTIONS: tuple[str, ...] = (
    "repo1-s3-endpoint",
    "repo1-s3-bucket",
    "repo1-s3-key",
    "repo1-s3-key-secret",
    "repo1-cipher-pass",
)


class SecretConfigError(ValueError):
    """The VM pgBackRest secret boundary is not safe to mount."""


def validate_secret_dir(
    path: Path, *, postgres_uid: int = 70, postgres_gid: int = 70,
) -> tuple[Path, ...]:
    """Validate the exact five secret options and return file paths only."""
```

The function never returns secret values. It rejects directory/file symlinks,
non-regular files, empty values, duplicate/unknown assignments, known example
markers, group/other write access, other-user read access, and a directory/file
that the supplied PostgreSQL UID/GID cannot traverse/read. Group read/traverse
is allowed only when the file/directory group is the supplied PostgreSQL GID;
the CLI prints only `secret_config_invalid` on failure.

### Restore baseline

`evidence.py` exposes:

```python
@dataclass(frozen=True, slots=True)
class RestoreBaseline:
    target_backup_label: str
    target_time: str | None
    database_name: str
    account_id: str
    environment: str
    projector_version: str
    migration_heads: tuple[str, ...]
    event_count: int
    event_head: int | None
    event_hash: str


def load_restore_baseline(
    path: Path, *, target_backup_label: str, target_time: str | None,
    account_id: str, environment: str, projector_version: str,
) -> RestoreBaseline:
    """Read a bounded JSON baseline and require exact request identity."""
```

The JSON file must be absolute, regular, ≤64 KiB, contain exactly the bounded
fields above, use a canonical UUID, a PostgreSQL identifier matching
`[A-Za-z_][A-Za-z0-9_]{0,62}`, strict non-negative integers, lowercase SHA-256
event hash, and migration-head identifiers matching `[A-Za-z0-9._-]{1,128}`.
`target_backup_label`, `target_time`, account, environment and projector must
match the current drill request byte-for-byte; missing baseline is fatal.

### Restore command plan

`restore_commands.py` changes `RestorePlan` to:

```python
@dataclass(frozen=True, slots=True)
class RestorePlan:
    project_name: str
    volume_name: str
    network_name: str
    egress_network_name: str
    container_name: str
    verifier_container_name: str
    sql_admin_role: str
    verify_role: str
    database_name: str
    account_id: str
    environment: str
    projector_version: str
    backup_label: str
    target_time: str | None
    expected_event_hash: str
    create_commands: tuple[tuple[str, ...], ...]
    run_commands: tuple[tuple[str, ...], ...]
    cleanup_commands: tuple[tuple[str, ...], ...]
```

`build_restore_plan(..., database_name: str, expected_event_hash: str, ...)`
validates the database identifier and lowercase hash in addition to the
existing account/environment/projector/label/run-id checks. `create_commands`
are, in order, internal network creation, egress network creation, and volume
creation. `run_commands` are Compose `up restore-db`, egress property inspect,
egress disconnect, post-disconnect container-network inspect, and Compose
`run --rm --no-deps --name <generated-verifier> verifier`; the verifier argv contains
`--expected-event-hash`. Every Compose command uses the absolute Compose path.
All cleanup commands contain only generated project/container/volume/network
names.

### Evidence renderer

`render_restore_evidence` changes to:

```python
def render_restore_evidence(
    *, schema_tsv: str, replay_json: str,
    baseline: RestoreBaseline, elapsed_seconds: int,
    observed_at_ms: int, config_path: Path, image_digest: str,
    image_labels: Mapping[str, str], network_name: str,
    network_internal: bool, egress_disconnected: bool,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Return measured restore evidence only after all independent gates pass."""
```

`image_digest` is the immutable local container `.Image` ID in exact
`sha256:<64 lowercase hex>` form. `image_labels` contains and validates the
three pinned labels from the image contract. The renderer emits
`observed_at_ms`, `target_time`, `egress_disconnected`, and `image_labels` in
addition to the existing bounded event/projection fields. `observed_at_ms`
must be a strict integer, not in the future, and no older than 900 seconds when
`now_ms` is supplied for deterministic tests.

## Task 1: Close the production archive, user, log, and failure-evidence boundary

**Files:**
- Modify: `docker-compose.bot.yml`
- Modify: `deploy/vm/pgbackrest/pgbackrest.conf`
- Modify: `deploy/vm/pgbackrest/status.sh`
- Modify: `deploy/vm/pgbackrest/preflight.sh`
- Modify: `deploy/vm/pgbackrest/backup.sh`
- Modify: `deploy/vm/pgbackrest/smoke.sh`
- Modify: `deploy/vm/pgbackrest/evidence.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_image.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_evidence.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_ops.py`

**Interfaces:** Consumes the existing pinned image/config and wrapper contracts. Produces `archive_timeout=60s`, explicit `--user postgres` execution, strict numeric evidence parsing, and failure reports that replace stale green evidence.

- [ ] **Step 1: Write failing contract tests.**

Add assertions that `docker-compose.bot.yml` contains `archive_timeout=60s`,
the tracked pgBackRest config contains all three `log-level-*=off` options, and
every production wrapper source contains `docker exec --user postgres` while
not containing a root-form `docker exec "$CONTAINER"`. Add an evidence test
that passes JSON numeric `1.0` and expects `EvidenceError`, and a wrapper test
that pre-creates a measured green `backup.json`, forces check/status failure,
then asserts the replacement has `measured:false` and no old `rpo_seconds`.

- [ ] **Step 2: Run the focused tests and verify the expected failures.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_image.py tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_ops.py -q
```

Expected: failures for missing archive timeout/log options, root-form execs,
float truncation, and stale evidence retention.

- [ ] **Step 3: Implement the minimum production and evidence changes.**

Add the exact PostgreSQL command item `archive_timeout=60s`. Set the three
pgBackRest log levels to `off`. Add `--user postgres` to status psql/info,
preflight check, backup, smoke, and any wrapper-owned pgBackRest invocation.
Change `_nonnegative_int` to accept only `int` or a decimal string matching
`(?:0|[1-9][0-9]*)`; reject floats, booleans, signs, whitespace and truncation.
Add `pgbackrest_check_failed` to the backup error-code allowlist and make
preflight/backup call the bounded failure writer before returning their saved
nonzero status. No failure branch may print captured command output.

- [ ] **Step 4: Run the focused tests and syntax checks.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_image.py tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_ops.py -q
cd ..
for file in deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/smoke.sh; do bash -n "$file"; done
```

Expected: all focused contracts pass.

- [ ] **Step 5: Commit the production boundary.**

```bash
git add docker-compose.bot.yml deploy/vm/pgbackrest/pgbackrest.conf deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/smoke.sh deploy/vm/pgbackrest/evidence.py backend_py/tests/scripts/test_offsite_dr_image.py backend_py/tests/scripts/test_offsite_dr_evidence.py backend_py/tests/scripts/test_offsite_dr_ops.py
git commit -m "🐛 Fix: harden pgBackRest archive and evidence boundary"
```

## Task 2: Share exact VM secret validation between deploy and restore

**Files:**
- Create: `deploy/vm/pgbackrest/secret_validation.py`
- Modify: `scripts/deploy-vm.sh`
- Modify: `deploy/vm/pgbackrest/restore_drill.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_ops.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_restore.py`

**Interfaces:** Consumes the VM `conf.d` directory and tracked config path. Produces one validator used by deploy and restore preflight, with no returned secret values.

- [ ] **Step 1: Write failing validator tests.**

Add cases for a valid single `r2.conf` containing exactly the five required
options, duplicate `repo1-s3-key`, missing endpoint, unknown assignment,
empty value, `<ACCOUNT_ID>`/`example` marker, symlink file, group-readable file,
other-readable file, directory not traversable by UID/GID 70, and a valid file
readable by the configured PostgreSQL UID/GID. Assert exception text is exactly
`secret_config_invalid` and never contains a secret value.

- [ ] **Step 2: Run validator tests and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py tests/scripts/test_offsite_dr_restore.py -q -k secret
```

Expected: import/validation failures because the shared module and callers do
not yet exist.

- [ ] **Step 3: Implement `secret_validation.py`.**

Walk only direct entries in the supplied directory; require regular non-symlink
`.conf` files. Each file has exactly one approved `[global]` section; reject
assignments outside it, wrong/extra/repeated sections and ambiguous syntax.
Parse blank/comment lines, accept exactly the five option names,
reject every other assignment and duplicate, and treat values as opaque for
validation. Require non-empty values, no case-insensitive marker matching
`example|placeholder|change[-_ ]?me|replace[-_ ]?me|<[^>]+>`, no group/other
write bits or other-user read bits, and owner/group read/traverse permission
for the supplied PostgreSQL UID/GID. Return only sorted `Path` objects. The
CLI must print only `secret_config_invalid` and return 2 on any
`SecretConfigError`.

- [ ] **Step 4: Replace duplicated shell/Python checks with the shared validator.**

In `scripts/deploy-vm.sh`, retain the tracked-clean config checks and call
`python3 "$ROOT/deploy/vm/pgbackrest/secret_validation.py" --secret-dir
"$PGBACKREST_SECRET_DIR"`; expand the tracked scan to endpoint, bucket, key,
secret-key and cipher-pass assignments with non-empty values. In
`restore_drill.py`, require the config to be tracked and clean, then load and
call the validator before creating any network, volume or Compose command.

- [ ] **Step 5: Run focused contracts, syntax and commit.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py tests/scripts/test_offsite_dr_restore.py -q -k secret
cd ..
bash -n scripts/deploy-vm.sh deploy/vm/pgbackrest/restore-drill.sh
python3 -m py_compile deploy/vm/pgbackrest/secret_validation.py
git add deploy/vm/pgbackrest/secret_validation.py scripts/deploy-vm.sh deploy/vm/pgbackrest/restore_drill.py backend_py/tests/scripts/test_offsite_dr_ops.py backend_py/tests/scripts/test_offsite_dr_restore.py
git commit -m "🐛 Fix: validate pgBackRest secret boundary consistently"
```

## Task 3: Implement the staged DR networks and corrected Compose model

**Files:**
- Modify: `docker-compose.dr.yml`
- Modify: `deploy/vm/pgbackrest/restore_commands.py`
- Modify: `deploy/vm/pgbackrest/restore_drill.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_restore.py`

**Interfaces:** Consumes the validated request and secret/config preflight. Produces generated internal/egress networks, a valid `restore-data` external volume mapping, and a verifier command that cannot start before egress disconnect.

- [ ] **Step 1: Write failing Compose and command-builder tests.**

Assert the Compose file declares only the logical volume key `restore-data`
with `external: true` and `name: ${DR_VOLUME_NAME}`, declares external `dr`
and `r2-egress` networks, mounts restore-db on both and verifier on `dr` only,
uses argv `pg_isready -U ${DR_SQL_ADMIN_ROLE} -d ${DR_DATABASE_NAME}` with SQL role `bfx` and the validated existing baseline database, and contains no `POSTGRES_*`,
`.env.runtime`, production env file, port or `BFX_VAULT_KEK` reference. Extend
the plan test to assert `egress_network_name`, ordered create commands with
exactly one `--internal`, a disconnect command before verifier, and cleanup of
both networks without `bfx_pgdata`.

- [ ] **Step 2: Run the focused restore tests and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_restore.py -q
```

Expected: failures for the `${DR_VOLUME_NAME}` mount, missing egress network,
old POSTGRES contract, and absent disconnect command.

- [ ] **Step 3: Correct the Compose model.**

Use this exact resource shape:

```yaml
volumes:
  restore-data:
    external: true
    name: ${DR_VOLUME_NAME}
networks:
  dr:
    external: true
    name: ${DR_NETWORK_NAME}
  r2-egress:
    external: true
    name: ${DR_EGRESS_NETWORK_NAME}
```

Set restore-db networks to `[dr, r2-egress]`, verifier networks to `[dr]`,
mount `restore-data:/var/lib/postgresql`, and remove `POSTGRES_USER`,
`POSTGRES_PASSWORD`, and `POSTGRES_DB`. Keep only generated DR restore inputs
on restore-db. The healthcheck uses SQL admin role `bfx` and the validated
existing baseline database. The runner additionally requires `pg_is_in_recovery()`
to return false before disconnect; connection acceptance is insufficient.

- [ ] **Step 4: Extend `RestorePlan` and command construction.**

Derive `egress_network_name` and generated `verify_role` from the validated run
ID. Create commands must be:

```text
docker network create --internal <internal-network>
docker network create <egress-network>
docker volume create <volume>
```

The plan must inspect the egress network and require `.Internal=false`, then
run `docker network disconnect <egress-network> <container>`, inspect the
container network membership to require exactly the generated internal network, and only then run
`docker compose run --rm --no-deps --name <generated-verifier> verifier`. Cleanup must independently force-remove the named verifier, then remove the Compose
restore-db container, volume, egress network, then internal network. Every generated
argument is validated against `^bfx-dr-[a-z0-9-]+$`; the Compose path is
absolute and all commands remain argv tuples without `shell=True`.

- [ ] **Step 5: Run focused tests and commit.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_restore.py -q
cd ..
python3 -m py_compile deploy/vm/pgbackrest/restore_commands.py
git add docker-compose.dr.yml deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py backend_py/tests/scripts/test_offsite_dr_restore.py
git commit -m "🐛 Fix: stage isolated restore R2 egress"
```

## Task 4: Add restored-cluster bootstrap and expected-state baseline gates

**Files:**
- Modify: `deploy/vm/pgbackrest/evidence.py`
- Modify: `deploy/vm/pgbackrest/restore_commands.py`
- Modify: `deploy/vm/pgbackrest/restore_drill.py`
- Modify: `backend_py/scripts/verify_projection_replay.py` only if its bounded report needs a missing strict field; do not change replay semantics.
- Test: `backend_py/tests/scripts/test_offsite_dr_evidence.py`
- Test: `backend_py/tests/scripts/test_offsite_dr_restore.py`

**Interfaces:** Consumes a bounded baseline JSON and a healthy restored PostgreSQL cluster. Produces an ephemeral verifier login, exact expected hash argv, and field-by-field state comparison.

- [ ] **Step 1: Write failing baseline and bootstrap tests.**

Add a baseline fixture with:

```json
{
  "target_backup_label": "20260904031700-F",
  "target_time": null,
  "database_name": "bfx",
  "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
  "environment": "prod",
  "projector_version": "projector-v3",
  "migration_heads": ["head-a", "head-b"],
  "event_count": 9,
  "event_head": 42,
  "event_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Assert `load_restore_baseline` rejects a target/account/hash/count/head/migration
mismatch, non-absolute or >64 KiB file, float event count, and malformed DB
identifier. Update the fake command runner to accept `input_text` and assert the
bootstrap command includes `docker exec --user postgres --interactive`, while
the password occurs only in stdin and never in the argv tuple, report or log.
Assert the verifier argv contains `--expected-event-hash` and the baseline hash.

- [ ] **Step 2: Run the new tests and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_restore.py -q -k 'baseline or bootstrap or expected'
```

Expected: failures because the baseline dataclass/loader, database field,
bootstrap command and expected hash propagation are absent.

- [ ] **Step 3: Implement bounded baseline loading and evidence comparison.**

Implement `RestoreBaseline` and `load_restore_baseline` with exact identity
matching and strict scalar checks. Change `render_restore_evidence` to compare
schema migration heads/event count and replay event head/hash against the
baseline; reject every mismatch before returning. Pass the expected hash to
`verify_projection_replay.py replay`; retain all existing projection diagnostic
match checks. Add `target_time`, `observed_at_ms`, and `egress_disconnected` to
the bounded output only after validation.

- [ ] **Step 4: Implement local-socket role bootstrap without POSTGRES init.**

After restore-db health, poll `SELECT pg_is_in_recovery();` through the local
socket as OS user `postgres`, SQL role `bfx`, on the existing baseline database.
Require false within the same global deadline, then disconnect egress, require
exact internal membership, and bootstrap with that same SQL admin role. Use a generated role name and password; send the
password only in `stdin` to `psql`, never in argv. The SQL must validate the
baseline database exists, create the generated login role, grant only
`CONNECT`, `TEMPORARY`, schema `public` `USAGE`, and `SELECT` on
`event_log`, the eight fixed projection tables, and `alembic_version`. Build
the `DATABASE_URL` from the generated role/password/database for the verifier
Compose env file, mode `0600`. The schema query also uses
`docker exec --user postgres` against the baseline database. Do not create a
new application database and do not mount any application secret file.

- [ ] **Step 5: Run focused tests and commit.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_restore.py -q
cd ..
python3 -m py_compile deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py
git add deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py backend_py/tests/scripts/test_offsite_dr_evidence.py backend_py/tests/scripts/test_offsite_dr_restore.py
git commit -m "🐛 Fix: verify restored cluster against baseline"
```

## Task 5: Harden restore image provenance, deadlines, freshness, and cleanup

**Files:**
- Modify: `deploy/vm/pgbackrest/restore_drill.py`
- Modify: `deploy/vm/pgbackrest/evidence.py`
- Modify: `backend_py/tests/scripts/test_offsite_dr_restore.py`
- Modify: `backend_py/tests/scripts/test_offsite_dr_evidence.py`
- Modify: `backend_py/tests/scripts/test_offsite_dr_ops.py`

**Interfaces:** Consumes the staged plan, baseline gate and strict evidence renderer. Produces a bounded full-lifecycle deadline, immutable local image evidence, fresh timestamps, and failure-overwrites-success semantics.

- [ ] **Step 1: Write failing lifecycle tests.**

Add tests that the runner obtains image ID from
`docker inspect --format={{.Image}} <container>` rather than
`RepoDigests`, validates the three fixed image labels, passes observed target
time/timestamp to the renderer, disconnects egress before the verifier command,
and gives every restore command a positive remaining timeout. Add a runner that
blocks on schema/image/Compose commands and assert it is interrupted before
3600 seconds. Add a cleanup observer that fails cleanup and assert
`restore.json` is replaced by `measured:false` with `cleanup_failed`, not left
as a green report. Add stale/future timestamp renderer tests.

- [ ] **Step 2: Run focused lifecycle tests and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_restore.py tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_ops.py -q -k 'image or deadline or cleanup or freshness or egress'
```

Expected: failures for RepoDigests-only behavior, absent label/timestamp,
unbounded commands, stale success after cleanup failure, and missing egress
ordering.

- [ ] **Step 3: Implement one monotonic deadline and independent cleanup.**

Start the 3600-second deadline immediately before the first generated resource
command. Every create, Compose, health, bootstrap, schema, disconnect, image
inspect, verifier, renderer-adjacent command receives remaining time; health may
use a 600-second sub-deadline but never exceed the global deadline. Catch
`TimeoutExpired` as `restore_command_failed`. Cleanup uses a separate 30-second
deadline and attempts only resources whose creation/cleanup eligibility is
known; cleanup runs even when the global deadline expires. Unlink the temporary
env file within the cleanup deadline and return nonzero on cleanup failure.

- [ ] **Step 4: Implement image/timestamp/failure evidence gates.**

Read the restored container immutable `.Image` ID and fixed OCI labels, reject
missing/malformed/mismatched labels, and pass them to the renderer. Capture
`observed_at_ms` only after verifier/baseline validation and include the exact
requested `target_time` (including JSON null). Require `egress_disconnected is
True` before measured success. Keep the rendered success report in memory while
cleanup runs. Publish it atomically as `measured:true` only after cleanup
succeeds. If cleanup fails, atomically write a bounded `measured:false`
`cleanup_failed` report; if cleanup, interruption, persistence, or invalidation
prevents that, leave the evidence unavailable and retain only the bounded
`restore.log` marker.

- [ ] **Step 5: Add artifact freshness validation without changing Halt 2 state transitions.**

Make `_read_dr_measurement()` reject missing/non-strict `observed_at_ms`, future
timestamps, and reports older than the configured 900-second DR evidence window
when reading backup/restore artifacts. Keep its existing measured/seconds gates
and leave all Halt 2 transition logic unchanged. Update its unit fixtures to
include deterministic current timestamps and add stale/future rejection cases.

- [ ] **Step 6: Run focused tests, syntax checks and commit.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_restore.py tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_ops.py tests/scripts/test_halt2_cutover.py -q
cd ..
python3 -m py_compile deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py
git add deploy/vm/pgbackrest/restore_drill.py deploy/vm/pgbackrest/evidence.py backend_py/tests/scripts/test_offsite_dr_restore.py backend_py/tests/scripts/test_offsite_dr_evidence.py backend_py/tests/scripts/test_offsite_dr_ops.py backend_py/scripts/halt2_cutover.py backend_py/tests/scripts/test_halt2_cutover.py
git commit -m "🐛 Fix: bound restore lifecycle and evidence freshness"
```

## Task 6: Update operator runbook and architecture contracts

**Files:**
- Modify: `docs/runbooks/offsite-dr.md`
- Modify: `backend_py/ARCHITECTURE.md`
- Modify: `docs/runbooks/halt-1-exchange-account-cutover.md`
- Modify: `backend_py/tests/scripts/test_offsite_dr_ops.py`

**Interfaces:** Consumes all corrected commands/evidence fields. Produces an operator sequence that cannot enable timers before the explicit smoke/restore gates or imply a production restore.

- [ ] **Step 1: Write failing documentation contract tests.**

Assert the runbook orders: install four unit files with `install -m 0644`,
`systemctl daemon-reload`, `systemctl enable/start/list-timers`, create/validate
secret, build image, `stanza-create`, `check`, full, diff, info/verify,
`baseline.json`, staged isolated restore with `--baseline`, measured evidence,
then timer enablement. Assert it documents `restore-data`, temporary R2 egress
disconnect, existing database/local OS `postgres` / SQL `bfx` bootstrap, `--user postgres`,
`archive_timeout=60s`, and no production `bfx_pgdata` restore or Bitfinex request.

- [ ] **Step 2: Run documentation tests and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py -q -k 'runbook or architecture'
```

Expected: failures for missing staged restore/bootstrap/installation/order
language.

- [ ] **Step 3: Update the runbook with exact operator sequence.**

Document the five-option secret fragment and permission requirements without
printing example secret values. Add a bounded baseline JSON schema and state
that it must be captured for the same backup/PITR target; missing or mismatched
baseline fails the drill. Use the command:

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --account-id <canonical-uuid> \
  --environment prod \
  --projector-version execution-state-v1 \
  --backup-label <label> \
  --baseline /absolute/path/baseline.json
```

Explain that restore-db alone has temporary R2 egress, which is disconnected
before verifier; verifier has no R2/app secrets and uses an ephemeral role on
the existing restored database. Place Linux unit installation and
`systemctl daemon-reload` before enablement, and place the real R2
stanza/check/full/diff/info/verify/disposable expire and isolated restore gates
before declaring DR ready.

- [ ] **Step 4: Update architecture/Halt 1 links and commit.**

Record the approved staged boundary and evidence freshness rule in the backend
architecture source of truth, link the runbook from Halt 1 backup/PITR guidance,
and ensure no personal second-brain path, credential, identity or destructive
production volume command is introduced.

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py -q
cd ..
git diff --check
git add docs/runbooks/offsite-dr.md backend_py/ARCHITECTURE.md docs/runbooks/halt-1-exchange-account-cutover.md backend_py/tests/scripts/test_offsite_dr_ops.py
git commit -m "📝 Docs: document staged offsite DR hardening"
```

## Final verification handoff

After Task 6, run the full repository gate from `backend_py/`, build and inspect
the custom image without starting production, run Compose/Bash/systemd static
checks, and record systemd/R2 external acceptance as measured or unavailable.
Then generate the SDD review package against the branch merge-base and use the
requesting-code-review skill. Offline green status must not be reported as R2
reachability, restore success, or production DR readiness.
