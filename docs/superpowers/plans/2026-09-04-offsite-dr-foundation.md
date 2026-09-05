# Offsite DR Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在現有 OCI VM PostgreSQL 18 stack 上交付一套可測試的 pgBackRest + Cloudflare R2 異地備份、狀態 evidence 與 isolated restore foundation。

**Architecture:** 以 pinned custom PostgreSQL image 安裝 pgBackRest 2.59.1，讓 archive_command、base backup 與 restore 使用同一個 binary/config；R2 credentials 只由 VM 上的 secret fragment 注入。VM systemd timer 只負責呼叫小型 wrapper，status/preflight 產生與目前 Halt 2 parser 相容的 bounded JSON，restore drill 以獨立 volume 和 internal: true network 驗證 event-only replay，永不碰 production volume 或 Bitfinex client。

**Tech Stack:** PostgreSQL 18 Alpine, pgBackRest 2.59.1, Cloudflare R2 S3 API, Docker Compose, Bash, Python 3.13 standard library, systemd, pytest, PyYAML。

**Spec:** docs/superpowers/specs/2026-09-04-offsite-dr-cloudflare-r2-design.md

## Global Constraints

- PostgreSQL base image 固定為 postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2。
- pgBackRest 固定為 2.59.1 distribution tarball，build 時核對 SHA-256 1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d。
- R2 使用 S3 region auto、path URI style、private Standard bucket、/pgbackrest prefix 與 bucket-scoped Object Read & Write token。
- pgBackRest 固定 repo1-block=y、repo1-bundle=y、archive-async=y、start-fast=y、repo1-cipher-type=aes-256-cbc。
- operational target 是 RPO ≤300 秒、restore drill RTO ≤3600 秒；Halt 2 canary 的 restore gate 仍是 RTO ≤60 秒。
- bfx_pgdata 是 production-only；任何 restore command、temporary volume、temporary network 都必須拒絕使用它。
- R2 endpoint、bucket、access key、secret key、repository cipher passphrase 不進 git、CI variables、shell history、Docker image layer 或 evidence JSON。
- 所有 Bash wrapper 使用 set -euo pipefail；外部 command failure、malformed output、missing credential、stale archive 與 verifier failure 一律 nonzero。
- 不修改 application event schema、projection implementation、venue reconcile、Halt 2 state machine，不自動 restore/resume/delete production data，不對 Bitfinex 發 request。
- 先寫 offline contract tests，再寫 implementation；預設 tests 不需要 R2 credential、Docker daemon 或 production database。
- 完成時必須從 backend_py/ 執行 uv run pytest -m "not integration"、uv run mypy src/、uv run ruff check，另做 Compose、Bash、systemd syntax checks。

---

## File map

### Runtime image and Compose

- Create deploy/vm/postgres/Dockerfile：build pinned pgBackRest distribution tarball，安裝 runtime libraries、jq 與 restore entrypoint，寫入 image labels。
- Create deploy/vm/postgres/dr-restore-entrypoint.sh：僅接受 bfx-dr- temporary volume，執行 pgBackRest restore 後以 archive_mode=off 啟動 isolated PostgreSQL。
- Create deploy/vm/pgbackrest/pgbackrest.conf：tracked non-secret repository、archive、retention、encryption 與 PostgreSQL path 設定。
- Modify docker-compose.bot.yml：使用 custom PostgreSQL image，掛載 tracked config、VM secret directory、spool/log/evidence directories，開啟 WAL archive；保留既有 healthcheck、app dependency 與 autoheal boundary。
- Create docker-compose.dr.yml：restore DB 與 verifier 的 internal-only Compose project；所有 volume/network 名稱由 restore_drill.py 建立後以 external resource 注入。

### Operator scripts and evidence

- Create deploy/vm/pgbackrest/evidence.py：pure standard-library parser/renderer，固定 backup/restore evidence schema，禁止輸出 raw command output。
- Create deploy/vm/pgbackrest/status.sh：read-only archive/pgBackRest status collector。
- Create deploy/vm/pgbackrest/preflight.sh：執行一次 pgbackrest check，再以 status collector 驗證 RPO。
- Create deploy/vm/pgbackrest/backup.sh：接受 --scheduled 或明確的 --type full|diff，只呼叫 pgBackRest data-plane。
- Create deploy/vm/pgbackrest/smoke.sh：需要明確確認旗標的 operator-only R2 lifecycle smoke。
- Create deploy/vm/pgbackrest/restore_commands.py：restore input validation、resource naming、Docker command builder 與 bounded cleanup command。
- Create deploy/vm/pgbackrest/restore_drill.py：執行 isolated restore、schema query、existing replay CLI、evidence rendering 與 failure log retention。
- Create deploy/vm/pgbackrest/restore-drill.sh：set -euo pipefail launcher，將 arguments 交給 restore_drill.py。
- Modify scripts/deploy-vm.sh：在 build/up 前檢查 config、secret boundary、Compose contract 與 custom image labels；不自動建立 R2 resource。
- Create deploy/vm/systemd/bfx-pgbackrest-backup.service and .timer。
- Create deploy/vm/systemd/bfx-pgbackrest-status.service and .timer。

### Documentation and tests

- Create backend_py/tests/scripts/test_offsite_dr_image.py：image/config/production Compose contracts。
- Create backend_py/tests/scripts/test_offsite_dr_evidence.py：parser、bounded output、fail-closed RPO/RTO contracts。
- Create backend_py/tests/scripts/test_offsite_dr_restore.py：Compose isolation、resource naming、command-builder 與 failure cleanup contracts。
- Create backend_py/tests/scripts/test_offsite_dr_ops.py：wrapper、systemd、deploy preflight、smoke/runbook ordering contracts。
- Create docs/runbooks/offsite-dr.md：operator bootstrap、R2 policy、secret file、timer、smoke、restore、rotation 與 incident handling。
- Modify backend_py/ARCHITECTURE.md：記錄 DR foundation、evidence boundary 與 restore 不等於 venue rollback。
- Modify docs/runbooks/halt-1-exchange-account-cutover.md：將 backup/PITR 操作指向 offsite DR runbook。

## Stable interfaces

後續 task 只能依賴下列已固定的介面，避免 shell parsing 和 evidence schema 在 task 之間漂移。

### Evidence renderer

deploy/vm/pgbackrest/evidence.py 提供：

    class EvidenceError(ValueError):
        """Input is unavailable, malformed, stale, or fails a DR gate."""


    def render_backup_evidence(
        *,
        archiver_tsv: str,
        info_json: str,
        config_path: Path,
        rpo_limit_seconds: int,
    ) -> dict[str, object]:
        """Return bounded measured backup evidence or raise EvidenceError."""


    def render_restore_evidence(
        *,
        schema_tsv: str,
        replay_json: str,
        target_backup_label: str,
        elapsed_seconds: int,
        config_path: Path,
        image_digest: str,
        network_name: str,
        network_internal: bool,
    ) -> dict[str, object]:
        """Return bounded measured restore evidence or raise EvidenceError."""


    def render_failure_evidence(
        *,
        kind: Literal["backup", "restore"], error_code: str, observed_at_ms: int,
    ) -> dict[str, object]:
        """Return a measured=false report with only a bounded error code."""

render_backup_evidence consumes one TSV row in this order: observed_at_ms, last_archived_at_ms, last_archived_wal, failed_count, last_failed_at_ms。 It consumes pgBackRest info --output=json with stanza name bfx, backup labels, backup types and stop epochs. It emits schema_version, measured, rpo_seconds, observed_at_ms, stanza, repository, last_archived_wal, latest_backup_label, config_digest, failed_archive_count。

render_restore_evidence accepts the existing verify_projection_replay.py replay JSON and one schema TSV row containing server_version_num, migration_heads, and account-scoped event count. It requires every known projection diagnostic_diff.matches value to be true, a valid event hash, non-negative counts, a valid bfx-dr- network name, network_internal=True, and a non-negative elapsed time. It emits only fixed identifiers, counts, hashes, timestamps, and booleans.

### Shell commands

    deploy/vm/pgbackrest/status.sh [--output /absolute/path/backup.json] [--require-rpo]
    deploy/vm/pgbackrest/preflight.sh [--output /absolute/path/backup.json]
    deploy/vm/pgbackrest/backup.sh --scheduled
    deploy/vm/pgbackrest/backup.sh --type full
    deploy/vm/pgbackrest/backup.sh --type diff
    deploy/vm/pgbackrest/smoke.sh --confirm-r2-smoke
    deploy/vm/pgbackrest/restore-drill.sh \
      --account-id 3f19d046-5030-494c-9a0a-9573bb890c1f \
      --environment prod \
      --projector-version projector-v3 \
      --backup-label 20260904031700-F

All paths passed to these scripts must be absolute. Defaults resolve to the checkout’s deploy/vm/pgbackrest directory and $HOME/bfx/dr-evidence；restore-drill.sh accepts only the explicit account/environment/projector/backup arguments and an optional UTC ISO-8601 --target-time。

---

### Task 1: Build the pinned PostgreSQL/pgBackRest image and wire production Compose

**Files:**
- Create: deploy/vm/postgres/Dockerfile
- Create: deploy/vm/postgres/dr-restore-entrypoint.sh
- Create: deploy/vm/pgbackrest/pgbackrest.conf
- Modify: docker-compose.bot.yml
- Test: backend_py/tests/scripts/test_offsite_dr_image.py

**Interfaces:**
- Consumes: current postgres:18-alpine volume mount at /var/lib/postgresql, current docker-compose.bot.yml service names, and the global digests above.
- Produces: image bfx-postgres:local, executable /usr/local/bin/pgbackrest, executable /usr/local/bin/bfx-dr-restore, tracked config at /etc/pgbackrest/pgbackrest.conf, and the production service contract used by Tasks 2–5.

- [ ] **Step 1: Write failing image/config/Compose contract tests**

Create tests that load files as text/YAML and fail while the new files do not exist:

    import re
    from pathlib import Path

    import yaml

    ROOT = Path(__file__).resolve().parents[3]


    def test_postgres_image_has_the_resolved_digest_and_pgbackrest_checksum() -> None:
        dockerfile = (ROOT / "deploy/vm/postgres/Dockerfile").read_text()
        assert "FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2" in dockerfile
        assert "PG_BACKREST_VERSION=2.59.1" in dockerfile
        assert "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d" in dockerfile
        assert "sha256sum -c" in dockerfile
        assert "meson setup" in dockerfile
        assert "ninja -C" in dockerfile
        assert re.search(r"^RUN .*pgbackrest.* version", dockerfile, re.MULTILINE)


    def test_tracked_pgbackrest_config_contains_no_secret_options() -> None:
        config = (ROOT / "deploy/vm/pgbackrest/pgbackrest.conf").read_text()
        for option in ("repo1-s3-key=", "repo1-s3-key-secret=", "repo1-cipher-pass="):
            assert option not in config
        for option in (
            "repo1-type=s3", "repo1-path=/pgbackrest", "repo1-s3-region=auto",
            "repo1-s3-uri-style=path", "repo1-block=y", "repo1-bundle=y",
            "archive-async=y", "start-fast=y", "repo1-cipher-type=aes-256-cbc",
            "repo1-retention-full=4", "repo1-retention-diff=6",
            "pg1-path=/var/lib/postgresql/18/docker",
        ):
            assert option in config


    def test_production_postgres_service_uses_archive_image_and_keeps_autoheal_separate() -> None:
        compose = yaml.safe_load((ROOT / "docker-compose.bot.yml").read_text())
        postgres = compose["services"]["postgres"]
        assert postgres["image"] == "bfx-postgres:local"
        assert postgres["build"]["dockerfile"] == "deploy/vm/postgres/Dockerfile"
        assert "archive_mode=on" in " ".join(postgres["command"])
        assert "archive_command=pgbackrest --stanza=bfx archive-push %p" in " ".join(postgres["command"])
        assert any("/etc/pgbackrest/pgbackrest.conf" in item for item in postgres["volumes"])
        assert any("/etc/pgbackrest/conf.d" in item for item in postgres["volumes"])
        assert any("/var/spool/pgbackrest" in item for item in postgres["volumes"])
        assert "labels" not in postgres or postgres["labels"].get("autoheal") != "true"

- [ ] **Step 2: Run the new tests and verify the expected failure**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_image.py -q

Expected: FAIL because the custom image, config, and Compose wiring are not yet present.

- [ ] **Step 3: Implement the pinned image and non-secret config**

Use a two-stage Dockerfile based on the exact PostgreSQL digest in the global constraints. Download the official distribution tarball from https://github.com/pgbackrest/pgbackrest/releases/download/release%2F2.59.1/pgbackrest-2.59.1.tar.gz, verify it with the exact checksum before extraction, compile it with meson setup and ninja -C, run the distribution smoke suite during the build, then copy only the binary into the final PostgreSQL image. Install only the runtime libraries found by ldd, plus jq; remove the build toolchain from the final stage. Add labels for PostgreSQL base digest, pgBackRest version, and source checksum. Create /etc/pgbackrest/conf.d, /var/spool/pgbackrest, /var/log/pgbackrest, and /dr-evidence with the postgres user as owner before runtime bind mounts are applied.

Implement dr-restore-entrypoint.sh with these exact invariants:

    #!/usr/bin/env bash
    set -euo pipefail

    case "$DR_VOLUME_NAME" in
      bfx-dr-*) ;;
      *) echo "ERROR: restore volume is not a bfx-dr resource" >&2; exit 2 ;;
    esac

    [ "$DR_VOLUME_NAME" != bfx_pgdata ]
    : "$DR_TARGET_BACKUP_LABEL"

    rm -rf "$PGDATA"/*
    if [ -n "$DR_TARGET_TIME" ]; then
      pgbackrest --stanza=bfx --set="$DR_TARGET_BACKUP_LABEL" \
        --type=time --target="$DR_TARGET_TIME" --target-action=promote restore
    else
      pgbackrest --stanza=bfx --set="$DR_TARGET_BACKUP_LABEL" \
        --target-action=promote restore
    fi

    exec /usr/local/bin/docker-entrypoint.sh postgres \
      -c archive_mode=off -c archive_command=''

The final image must provide /usr/local/bin/docker-entrypoint.sh by copying the base image’s entrypoint to that path before replacing the container entrypoint. The restore entrypoint must never read app env files or invoke a Docker client.

Write pgbackrest.conf with [global] repository/archive/retention settings and [bfx] pg1-path, leaving endpoint, bucket, access key, secret key, and cipher passphrase to the mounted conf.d fragment. Set the Sunday full schedule in the operator wrapper, not in pgBackRest config.

- [ ] **Step 4: Wire Compose without changing app service contracts**

Change only the postgres service in docker-compose.bot.yml to build the custom image from the repository root, mount the tracked config read-only, mount $HOME/bfx/pgbackrest/conf.d read-only, and mount host directories for spool, logs, and bounded evidence. Keep the existing bfx_pgdata:/var/lib/postgresql, loopback port, restart policy, healthcheck, and all depends_on relationships unchanged. Use a list-form command containing exactly postgres, -c, archive_mode=on, -c, and archive_command=pgbackrest --stanza=bfx archive-push %p.

- [ ] **Step 5: Run the contract tests and syntax checks**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_image.py -q
    cd ..
    bash -n deploy/vm/postgres/dr-restore-entrypoint.sh
    docker compose -f docker-compose.bot.yml config --quiet

Expected: the contract tests, Bash syntax, and Compose parse pass. Do not start the production stack in this task.

- [ ] **Step 6: Commit the image/config unit**

    git add deploy/vm/postgres deploy/vm/pgbackrest/pgbackrest.conf docker-compose.bot.yml backend_py/tests/scripts/test_offsite_dr_image.py
    git commit -m "✨ Feat: add pinned pgBackRest postgres image"

---

### Task 2: Implement bounded status, preflight, backup, and RPO evidence

**Files:**
- Create: deploy/vm/pgbackrest/evidence.py
- Create: deploy/vm/pgbackrest/status.sh
- Create: deploy/vm/pgbackrest/preflight.sh
- Create: deploy/vm/pgbackrest/backup.sh
- Create: deploy/vm/pgbackrest/smoke.sh
- Create: backend_py/tests/scripts/test_offsite_dr_evidence.py
- Create: backend_py/tests/scripts/test_offsite_dr_ops.py

**Interfaces:**
- Consumes: Task 1’s custom image/config and current Halt 2 _read_dr_measurement() contract in backend_py/scripts/halt2_cutover.py.
- Produces: backup.json with measured:true only after archive and backup checks pass; status/preflight exit codes; manual smoke command sequence used by the runbook and isolated restore.

- [ ] **Step 1: Write failing evidence tests with fixed pgBackRest fixtures**

Add a standard-library fixture using the actual info --output=json structure:

    def _info_json() -> str:
        return json.dumps([{
            "name": "bfx",
            "backup": [
                {"label": "20260903031700-F", "type": "full",
                 "timestamp": {"start": {"epoch": 1756875000}, "stop": {"epoch": 1756875120}}},
                {"label": "20260904031700-D", "type": "diff",
                 "timestamp": {"start": {"epoch": 1756961220}, "stop": {"epoch": 1756961240}}},
            ],
        }])


    def test_backup_evidence_calculates_rpo_and_is_bounded(tmp_path: Path) -> None:
        config = tmp_path / "pgbackrest.conf"
        config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
        result = render_backup_evidence(
            archiver_tsv="1756961300000\t1756961240000\t00000001000000000000000A\t0\t",
            info_json=_info_json(), config_path=config, rpo_limit_seconds=300,
        )
        assert result["measured"] is True
        assert result["rpo_seconds"] == 60
        assert result["latest_backup_label"] == "20260904031700-D"
        assert "repo1-s3-key-secret" not in json.dumps(result)


    @pytest.mark.parametrize(
        ("archiver_tsv", "info_json", "error_code"),
        [
            ("1756961300000\t\t\t0\t", _info_json(), "archive_never_confirmed"),
            ("1756961300000\t1756961000000\twal\t0\t", _info_json(), "archive_lag_exceeded"),
            ("1756961300000\t1756961240000\twal\t0\t", "[]", "backup_set_missing"),
            ("not-json\trow", _info_json(), "archiver_output_invalid"),
        ],
    )
    def test_backup_evidence_fails_closed(tmp_path: Path, archiver_tsv: str, info_json: str, error_code: str) -> None:
        config = tmp_path / "pgbackrest.conf"
        config.write_text("[global]\n", encoding="utf-8")
        with pytest.raises(EvidenceError, match=error_code):
            render_backup_evidence(
                archiver_tsv=archiver_tsv, info_json=info_json,
                config_path=config, rpo_limit_seconds=300,
            )

The test loader may import evidence.py by file path because deploy/vm/pgbackrest is not a Python package. Add tests for render_failure_evidence that assert measured is false, error_code is allowlisted, and a TOKEN-SENTINEL supplied by a failed command is absent from the JSON.

- [ ] **Step 2: Run the evidence tests and verify they fail**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_evidence.py -q

Expected: FAIL because evidence.py and its parser functions do not yet exist.

- [ ] **Step 3: Implement the pure evidence renderer**

Implement evidence.py with no third-party imports. Use hashlib.sha256 to hash only the tracked config file. Parse the archiver TSV as exactly five fields and reject negative times, blank WAL when an archive time exists, a last failure newer than the last successful archive, missing full backup, malformed info JSON, and any RPO over the supplied limit. Parse the bfx stanza, select the latest backup by numeric stop epoch, and retain only label/type/timestamps needed for the bounded report.

Use this fixed error-code allowlist:

    BACKUP_ERROR_CODES = frozenset({
        "archive_never_confirmed", "archive_lag_exceeded", "backup_set_missing",
        "archiver_output_invalid", "pgbackrest_info_invalid", "config_unreadable",
    })
    RESTORE_ERROR_CODES = frozenset({
        "restore_output_invalid", "schema_output_invalid", "event_hash_invalid",
        "projection_replay_mismatch", "network_not_internal", "rto_invalid",
        "restore_command_failed", "cleanup_failed",
    })

The CLI accepts backup and restore subcommands, writes JSON atomically through a sibling temporary file, sets measured:true only for validated output, and on EvidenceError writes measured:false with one allowlisted error_code before returning exit code 2. Never include exception text, command output, connection URLs, or environment values in the report.

- [ ] **Step 4: Implement status and preflight with read-only command boundaries**

status.sh validates the container name with ^[A-Za-z0-9_.-]+$, creates a temporary directory with mktemp -d, and runs only a fixed SELECT against pg_stat_archiver plus docker exec "$CONTAINER" pgbackrest --stanza=bfx info --output=json. The SQL returns observed_at_ms, last_archived_at_ms, last_archived_wal, failed_count, and last_failed_at_ms; it contains no mutation or transaction statement. It passes the captured files and tracked config to evidence.py backup. --require-rpo returns nonzero for malformed/unavailable/stale output; a failed report is never converted to rpo_seconds=0.

preflight.sh runs exactly one docker exec invocation with pgbackrest --stanza=bfx check, then calls status.sh --require-rpo. It preserves the check’s nonzero status and never calls backup, restore, expire, resume, or halt APIs.

backup.sh accepts only --scheduled, --type full, or --type diff. --scheduled uses date -u +%u and selects full only for weekday 7, otherwise diff; it calls docker exec "$CONTAINER" pgbackrest --stanza=bfx --type="$TYPE" backup and then status.sh --require-rpo. It never retries with a different type or hides an exit status.

smoke.sh requires --confirm-r2-smoke and executes, in order, stanza-create, check, full backup, diff backup, info --output=json, and verify through docker exec. It is opt-in and is never called by systemd or unit tests.

- [ ] **Step 5: Add wrapper contract tests**

Extend test_offsite_dr_ops.py with these contracts:

    def test_pgbackrest_wrappers_fail_closed_and_have_no_mutating_sql() -> None:
        for name in ("status.sh", "preflight.sh", "backup.sh", "smoke.sh"):
            source = (ROOT / "deploy/vm/pgbackrest" / name).read_text()
            assert "set -euo pipefail" in source
            assert not re.search(
                r"\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b",
                source, re.I,
            )
        assert "--confirm-r2-smoke" in (ROOT / "deploy/vm/pgbackrest/smoke.sh").read_text()


    def test_status_is_read_only_and_does_not_restore_or_archive_push() -> None:
        source = (ROOT / "deploy/vm/pgbackrest/status.sh").read_text()
        assert "pg_stat_archiver" in source
        assert "info --output=json" in source
        assert "--require-rpo" in source
        assert "archive-push" not in source
        assert "restore" not in source

Add a subprocess test with a fake docker executable returning valid TSV/JSON. Assert rpo_seconds=60, exactly one SQL SELECT, and absence of TOKEN-SENTINEL from stdout, stderr, and evidence. Add the stale fixture and assert nonzero plus measured:false.

- [ ] **Step 6: Run focused tests and shell checks**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_evidence.py tests/scripts/test_offsite_dr_ops.py -q
    cd ..
    for file in deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/smoke.sh; do bash -n "$file"; done

Expected: all focused tests pass and every wrapper parses. Do not run smoke.sh.

- [ ] **Step 7: Commit the evidence unit**

    git add deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/smoke.sh backend_py/tests/scripts/test_offsite_dr_evidence.py backend_py/tests/scripts/test_offsite_dr_ops.py
    git commit -m "✨ Feat: add fail-closed pgBackRest evidence"

---

### Task 3: Implement the internal isolated restore drill

**Files:**
- Create: docker-compose.dr.yml
- Create: deploy/vm/pgbackrest/restore_commands.py
- Create: deploy/vm/pgbackrest/restore_drill.py
- Create: deploy/vm/pgbackrest/restore-drill.sh
- Create: backend_py/tests/scripts/test_offsite_dr_restore.py

**Interfaces:**
- Consumes: Task 1 image/entrypoint, Task 2 evidence.py, existing backend_py/scripts/verify_projection_replay.py replay, and an operator-selected pgBackRest backup label.
- Produces: an isolated restore run with restore.json accepted by halt2_cutover._read_dr_measurement() and a nonzero exit for every isolation, schema, replay, hash, or cleanup failure.

- [ ] **Step 1: Write failing restore isolation and command-builder tests**

Create YAML and pure command-builder tests:

    def test_dr_compose_is_internal_and_has_no_production_env_files() -> None:
        compose = yaml.safe_load((ROOT / "docker-compose.dr.yml").read_text())
        assert compose["networks"]["dr"]["internal"] is True
        assert "ports" not in compose["services"]["restore-db"]
        assert "ports" not in compose["services"]["verifier"]
        source = (ROOT / "docker-compose.dr.yml").read_text()
        for forbidden in ("bot.env", "webapi.env", "frontend.env", ".env.runtime", "BFX_VAULT_KEK"):
            assert forbidden not in source


    def test_restore_plan_names_are_random_prefixed_and_never_production() -> None:
        plan = build_restore_plan(
            account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
            environment="prod", projector_version="projector-v3",
            backup_label="20260904031700-F", target_time=None,
            run_id="20260904T031700Z-a1b2c3d4e5f60718",
        )
        assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.volume_name)
        assert plan.volume_name != "bfx_pgdata"
        assert plan.network_name.startswith("bfx-dr-")
        assert all("bfx_pgdata" not in " ".join(command) for command in plan.cleanup_commands)


    def test_restore_plan_rejects_command_injection_and_noncanonical_account() -> None:
        with pytest.raises(RestoreInputError):
            build_restore_plan(
                account_id="not-a-uuid", environment="prod", projector_version="v3",
                backup_label="20260904031700-F;rm", target_time=None,
                run_id="20260904T031700Z-a1b2c3d4e5f60718",
            )

Add a restore evidence fixture test with every diagnostic_diff.matches true and assert measured:true, rto_seconds, event hash, projection hashes, row counts, network assertion, and config/image digests. Add a fixture with one false projection match and assert EvidenceError("projection_replay_mismatch").

- [ ] **Step 2: Run restore tests and verify the expected failure**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_restore.py -q

Expected: FAIL because the DR Compose file and command-builder do not yet exist.

- [ ] **Step 3: Implement the isolated Compose project**

Create docker-compose.dr.yml with exactly two services:

- restore-db: bfx-postgres:local, generated DR_CONTAINER_NAME, generated DR_POSTGRES_USER/DR_POSTGRES_PASSWORD/DR_POSTGRES_DB, entrypoint /usr/local/bin/bfx-dr-restore, tracked pgBackRest config read-only, $HOME/bfx/pgbackrest/conf.d read-only, external temporary restore volume at /var/lib/postgresql, and an internal healthcheck using pg_isready.
- verifier: bfx-bot:local, no env_file, no ports, only generated DATABASE_URL plus BFX_DEPLOYMENT_ENV, with entrypoint python scripts/verify_projection_replay.py and default command replay. The runner appends the validated account UUID, environment, and projector version as argv values to docker compose run --rm verifier; it must depend on restore-db health and join only the dr network.

Declare dr as internal: true; declare the restore volume as external with name: $DR_VOLUME_NAME and the network as external with name: $DR_NETWORK_NAME. Do not use docker compose down -v; resource ownership is managed by the Python runner.

- [ ] **Step 4: Implement safe resource and Docker command construction**

restore_commands.py must use dataclasses and tuple command arguments, never shell=True, eval, string interpolation into a shell command, or docker compose down -v. Define:

    @dataclass(frozen=True, slots=True)
    class RestorePlan:
        project_name: str
        volume_name: str
        network_name: str
        container_name: str
        account_id: str
        environment: str
        projector_version: str
        backup_label: str
        target_time: str | None
        create_commands: tuple[tuple[str, ...], ...]
        run_commands: tuple[tuple[str, ...], ...]
        cleanup_commands: tuple[tuple[str, ...], ...]


    def build_restore_plan(
        *,
        account_id: str, environment: str, projector_version: str,
        backup_label: str, target_time: str | None, run_id: str,
    ) -> RestorePlan:
        """Validate all operator strings and return argv-safe Docker commands."""

Validate account UUID canonically, environment against prod, shadow, ci, projector version against [A-Za-z0-9._-]+, backup label against [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}, and run ID against [0-9TZ-]+-[a-f0-9]{16}. Derive volume/network/container/project names from bfx-dr- plus run ID and reject any name equal to or containing bfx_pgdata. The plan must include explicit docker network create --internal, docker volume create, Compose up, Compose run --rm verifier, Compose rm -sf restore-db, docker volume rm, and docker network rm commands with only generated names.

- [ ] **Step 5: Implement the Python drill runner and shell launcher**

restore_drill.py must:

1. Parse the exact arguments from the stable interface and validate the R2 secret directory and tracked config before creating resources.
2. Generate a UTC timestamp plus secrets.token_hex(8) run ID, a random database password, and a mode-0600 temporary Compose env file containing only DR resource names, database connection values, account/environment/projector values, target backup values, and the temporary secret directory path.
3. Start the monotonic RTO clock immediately before creating the temporary volume. Create only the plan’s network and volume, then run Compose restore-db.
4. Poll docker inspect --format={{.State.Health.Status}} for at most 600 seconds. On timeout, write a bounded failure report and exit nonzero.
5. Run the schema query through docker exec with psql -X -qAt -F '\t' -v ON_ERROR_STOP=1; query only PostgreSQL version, alembic_version, and account/environment event count.
6. Run the existing replay command in verifier, capture its single JSON report, require exit code 0, and reject any account/environment/projector mismatch or false diagnostic_diff.matches value.
7. Call render_restore_evidence, write restore.json, and stop the RTO clock only after evidence validation succeeds. A successful report has measured:true and rto_seconds equal to the integer monotonic elapsed seconds, rounded up to one second.
8. In a finally path, remove only the named Compose container, generated external volume, and generated external network. On failure retain restore.json with measured:false and an 8 KiB bounded, redacted log; cleanup errors return nonzero and use cleanup_failed without replacing the original evidence.

restore-drill.sh must contain only the fail-closed launcher:

    #!/usr/bin/env bash
    set -euo pipefail
    SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
    exec python3 "$SCRIPT_DIR/restore_drill.py" "$@"

- [ ] **Step 6: Add restore failure and secret-boundary tests**

Use a fake runner injected into restore_drill.py to assert that a verifier failure still issues cleanup only for generated names, leaves the evidence file, and never writes DR_POSTGRES_PASSWORD or TOKEN-SENTINEL into evidence/log output. Assert that a plan containing bfx_pgdata, a shell metacharacter in --backup-label, a non-internal network result, or a malformed replay hash fails before a production command can be emitted.

- [ ] **Step 7: Run focused tests and syntax checks**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_restore.py tests/scripts/test_offsite_dr_evidence.py -q
    cd ..
    bash -n deploy/vm/pgbackrest/restore-drill.sh deploy/vm/postgres/dr-restore-entrypoint.sh
    python3 -m py_compile deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py

Expected: all focused tests pass. Do not run the restore drill against a real bucket or production volume.

- [ ] **Step 8: Commit the restore unit**

    git add docker-compose.dr.yml deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py deploy/vm/pgbackrest/restore-drill.sh backend_py/tests/scripts/test_offsite_dr_restore.py
    git commit -m "✨ Feat: add isolated offsite restore drill"

---

### Task 4: Add systemd scheduling and deploy preflight integration

**Files:**
- Create: deploy/vm/systemd/bfx-pgbackrest-backup.service
- Create: deploy/vm/systemd/bfx-pgbackrest-backup.timer
- Create: deploy/vm/systemd/bfx-pgbackrest-status.service
- Create: deploy/vm/systemd/bfx-pgbackrest-status.timer
- Modify: scripts/deploy-vm.sh
- Modify: backend_py/tests/scripts/test_offsite_dr_ops.py

**Interfaces:**
- Consumes: Task 1 custom image labels and Task 2 backup.sh, status.sh, and current $HOME/bfx VM layout.
- Produces: daily backup selection at 03:17 UTC, five-minute read-only status collection, and deploy-time local validation that cannot silently omit the DR boundary.

- [ ] **Step 1: Write failing systemd/deploy contract tests**

Add tests that assert exact schedules and safety boundaries:

    def test_pgbackrest_timers_have_the_intended_utc_schedule() -> None:
        backup_timer = (ROOT / "deploy/vm/systemd/bfx-pgbackrest-backup.timer").read_text()
        status_timer = (ROOT / "deploy/vm/systemd/bfx-pgbackrest-status.timer").read_text()
        assert "OnCalendar=*-*-* 03:17:00 UTC" in backup_timer
        assert "OnCalendar=*:0/5" in status_timer
        assert "Persistent=true" in backup_timer
        assert "Persistent=true" in status_timer


    def test_services_are_one_shot_and_do_not_call_compose_run_or_autoheal() -> None:
        backup_service = (ROOT / "deploy/vm/systemd/bfx-pgbackrest-backup.service").read_text()
        status_service = (ROOT / "deploy/vm/systemd/bfx-pgbackrest-status.service").read_text()
        assert "Type=oneshot" in backup_service
        assert "Type=oneshot" in status_service
        assert "backup.sh --scheduled" in backup_service
        assert "status.sh" in status_service
        assert "docker compose run" not in backup_service + status_service
        assert "autoheal" not in backup_service.lower() + status_service.lower()


    def test_deploy_preflight_checks_secret_boundary_and_custom_image_labels() -> None:
        source = (ROOT / "scripts/deploy-vm.sh").read_text()
        for required in (
            "deploy/vm/pgbackrest/pgbackrest.conf",
            "pgbackrest/conf.d", "bfx-postgres:local",
            "org.opencontainers.image", "docker compose -f docker-compose.bot.yml config --quiet",
        ):
            assert required in source
        assert "BFX_VAULT_KEK" in source

- [ ] **Step 2: Run the tests and verify they fail**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_ops.py -q

Expected: FAIL because the four systemd units and deploy checks are not yet present.

- [ ] **Step 3: Implement the four systemd units**

Use User=ubuntu, WorkingDirectory=/home/ubuntu/bfx-funding-bot, Requires=docker.service, After=docker.service, Type=oneshot, and bounded TimeoutStartSec. The backup service executes:

    ExecStart=/home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest/backup.sh --scheduled

The status service executes:

    ExecStart=/home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest/status.sh --output /home/ubuntu/bfx/dr-evidence/backup.json

Use OnCalendar=*-*-* 03:17:00 UTC for backup and OnCalendar=*:0/5 for status, with Persistent=true and WantedBy=timers.target. Neither service may change halt state, start app services, restart PostgreSQL, perform restore, or remove R2 objects directly.

- [ ] **Step 4: Add deploy-vm local preflight**

After the existing environment-file checks and before docker compose -f docker-compose.bot.yml build, add checks for:

    PGBACKREST_CONFIG="$ROOT/deploy/vm/pgbackrest/pgbackrest.conf"
    PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"
    [ -r "$PGBACKREST_CONFIG" ] || { echo "ERROR: missing pgBackRest config" >&2; exit 1; }
    [ -d "$PGBACKREST_SECRET_DIR" ] || { echo "ERROR: missing pgBackRest secret directory" >&2; exit 1; }
    find "$PGBACKREST_SECRET_DIR" -type f -perm -0007 -print -quit | grep -q . && {
      echo "ERROR: pgBackRest secret file is accessible by other users" >&2
      exit 1
    }
    git grep -n -E 'repo1-s3-key(-secret)?=|repo1-cipher-pass=' -- . ':!docs/superpowers/specs/**' && {
      echo "ERROR: secret pgBackRest option is tracked" >&2
      exit 1
    } || true
    docker compose -f docker-compose.bot.yml config --quiet

The actual implementation must avoid printing secret file contents and must reject empty secret files or known example markers without exposing their values. After the existing Compose build, inspect bfx-postgres:local labels and fail unless base digest equals the global digest, pgBackRest version equals 2.59.1, and the source checksum equals the global checksum. Do not call R2, stanza-create, check, backup, restore, or expire from deploy-vm.sh; those remain explicit operator actions.

Create the non-secret runtime directories with install -d before Compose parsing: $HOME/bfx/pgbackrest/spool, $HOME/bfx/pgbackrest/log, and $HOME/bfx/dr-evidence. Assign ownership/permissions so the container postgres UID can write spool/log and the VM operator can write evidence; do not create or overwrite the secret fragment automatically.

- [ ] **Step 5: Run systemd/deploy tests and parser checks**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_ops.py -q
    cd ..
    for file in deploy/vm/pgbackrest/*.sh scripts/deploy-vm.sh; do bash -n "$file"; done
    systemd-analyze verify deploy/vm/systemd/bfx-pgbackrest-backup.service deploy/vm/systemd/bfx-pgbackrest-backup.timer deploy/vm/systemd/bfx-pgbackrest-status.service deploy/vm/systemd/bfx-pgbackrest-status.timer

If systemd-analyze is unavailable locally, retain the static assertions and run it in the VM image/CI environment; do not replace it with an unverified success claim.

- [ ] **Step 6: Commit the operations unit**

    git add deploy/vm/systemd/bfx-pgbackrest-backup.service deploy/vm/systemd/bfx-pgbackrest-backup.timer deploy/vm/systemd/bfx-pgbackrest-status.service deploy/vm/systemd/bfx-pgbackrest-status.timer scripts/deploy-vm.sh backend_py/tests/scripts/test_offsite_dr_ops.py
    git commit -m "✨ Feat: schedule offsite backup health checks"

---

### Task 5: Publish the operator runbook and architecture boundary

**Files:**
- Create: docs/runbooks/offsite-dr.md
- Modify: backend_py/ARCHITECTURE.md
- Modify: docs/runbooks/halt-1-exchange-account-cutover.md
- Modify: backend_py/tests/scripts/test_offsite_dr_ops.py

**Interfaces:**
- Consumes: Tasks 1–4 commands, evidence fields, systemd unit names, current Halt 1/Halt 2 rollback rules, and the Cloudflare R2 token policy.
- Produces: an operator-readable manual sequence that separates provisioning, measurement, restore drill, token rotation, cipher escrow, and venue rollback.

- [ ] **Step 1: Write failing documentation contract tests**

Assert that the new runbook contains the ordered control points and does not instruct a production restore:

    def test_offsite_runbook_contains_ordered_bootstrap_and_restore_controls() -> None:
        text = (ROOT / "docs/runbooks/offsite-dr.md").read_text()
        ordered = (
            "Create the private R2 bucket",
            "Create a bucket-scoped Object Read & Write token",
            "Create the VM secret fragment",
            "Build and validate bfx-postgres:local",
            "stanza-create",
            "pgbackrest --stanza=bfx check",
            "backup.sh --type full",
            "backup.sh --type diff",
            "Enable the backup and status timers",
            "Run an isolated restore drill",
            "Record measured RPO/RTO evidence",
        )
        cursor = -1
        for item in ordered:
            position = text.find(item, cursor + 1)
            assert position > cursor, item
            cursor = position
        assert "docker volume rm bfx_pgdata" not in text
        assert "docker compose down -v" not in text
        assert "Bitfinex" in text


    def test_architecture_documents_dr_is_not_venue_rollback() -> None:
        architecture = (ROOT / "backend_py/ARCHITECTURE.md").read_text()
        assert "pgBackRest" in architecture
        assert "restore" in architecture.lower()
        assert "venue rollback" in architecture.lower()

- [ ] **Step 2: Run the tests and verify the expected failure**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_ops.py -q

Expected: FAIL because the runbook and architecture section do not yet contain the new sequence.

- [ ] **Step 3: Write the operator runbook**

Document these sections in docs/runbooks/offsite-dr.md, using no real account, bucket, endpoint, or credential values:

1. Scope and gates: RPO 300 seconds, restore drill RTO 3600 seconds, Halt 2 canary RTO 60 seconds, measured evidence required, and no production restore from this foundation.
2. R2 provisioning: private Standard bucket, /pgbackrest prefix, bucket-scoped Object Read & Write token, no account-admin token, no S3 Object Lock assumption, and Bucket Lock only after testing expire.
3. VM secret boundary: create $HOME/bfx/pgbackrest/conf.d/r2.conf outside the repository with endpoint, bucket, key, secret, and cipher pass options; make it readable by the container postgres user and inaccessible to other users; keep the cipher passphrase in an offline escrow.
4. Bootstrap: build custom image, start/inspect PostgreSQL, run stanza-create, run check, run explicit full/diff smoke, inspect status.sh, and only then enable timers.
5. Scheduled operation: explain Sunday full/other-day diff at 03:17 UTC, status every five minutes, fail-closed meaning, evidence directory, and the fact that status failure does not autoheal PostgreSQL.
6. Isolated restore: require current halt/reconcile policy, operator-selected backup label, restore-drill.sh arguments, expected event head/hash and empty-projector replay match, evidence file, and cleanup behavior.
7. Rotation and incident response: R2 token rotation and cipher rotation are separate procedures; archive/auth failure keeps the system halted and triggers investigation; do not restore production to undo a venue write; follow rollback-after-venue-write.md.

Use copyable commands only for non-secret values:

    docker compose -f docker-compose.bot.yml build postgres
    docker exec bfx-postgres pgbackrest --stanza=bfx stanza-create
    docker exec bfx-postgres pgbackrest --stanza=bfx check
    deploy/vm/pgbackrest/backup.sh --type full
    deploy/vm/pgbackrest/backup.sh --type diff
    deploy/vm/pgbackrest/preflight.sh --output "$HOME/bfx/dr-evidence/backup.json"
    sudo systemctl enable --now bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer

- [ ] **Step 4: Update architecture and Halt 1 references**

Add a short source-of-truth section to backend_py/ARCHITECTURE.md stating that PostgreSQL WAL/base backups use pgBackRest to R2, evidence is bounded and consumed by Halt 2, and isolated restore is a data-integrity measurement only. Add a link to docs/runbooks/offsite-dr.md from the existing Halt 1 backup/PITR instructions. Do not add second-brain paths, credentials, private identities, or personal workflow notes to tracked project files.

- [ ] **Step 5: Run documentation contracts and Markdown hygiene checks**

    cd backend_py
    uv run pytest tests/scripts/test_offsite_dr_ops.py -q
    cd ..
    git diff --check
    rg -n -i "decision pending|not specified|fill in" docs/runbooks/offsite-dr.md backend_py/ARCHITECTURE.md

Expected: the tests and diff check pass; the forbidden-marker scan returns no matches. A real secret, raw token, production volume deletion command, or Bitfinex request in the runbook is a test failure and must be removed.

- [ ] **Step 6: Commit the documentation unit**

    git add docs/runbooks/offsite-dr.md backend_py/ARCHITECTURE.md docs/runbooks/halt-1-exchange-account-cutover.md backend_py/tests/scripts/test_offsite_dr_ops.py
    git commit -m "📝 Docs: publish offsite DR operator runbook"

---

### Task 6: Run the complete offline gate and request code review

**Files:**
- Test: backend_py/tests/scripts/test_offsite_dr_image.py
- Test: backend_py/tests/scripts/test_offsite_dr_evidence.py
- Test: backend_py/tests/scripts/test_offsite_dr_restore.py
- Test: backend_py/tests/scripts/test_offsite_dr_ops.py
- Verify: all files from Tasks 1–5

**Interfaces:**
- Consumes: the complete implementation and all task-level commits.
- Produces: fresh verification evidence, a list of any unmeasured external acceptance items, and a review-ready branch without claiming production DR readiness.

- [ ] **Step 1: Run all required offline tests from the correct directory**

    cd backend_py
    uv run pytest -m "not integration"

Expected: PASS with no integration, live, or benchmark tests selected.

- [ ] **Step 2: Run type, lint, shell, Compose, and systemd checks**

    uv run mypy src/
    uv run ruff check
    cd ..
    for file in deploy/vm/postgres/dr-restore-entrypoint.sh deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/smoke.sh deploy/vm/pgbackrest/restore-drill.sh scripts/deploy-vm.sh; do bash -n "$file"; done
    docker compose -f docker-compose.bot.yml config --quiet
    python3 -m py_compile deploy/vm/pgbackrest/evidence.py deploy/vm/pgbackrest/restore_commands.py deploy/vm/pgbackrest/restore_drill.py
    systemd-analyze verify deploy/vm/systemd/bfx-pgbackrest-backup.service deploy/vm/systemd/bfx-pgbackrest-backup.timer deploy/vm/systemd/bfx-pgbackrest-status.service deploy/vm/systemd/bfx-pgbackrest-status.timer

Expected: every command exits zero. docker-compose.dr.yml requires operator-generated interpolation values, so its offline contract is the YAML/test contract; it is fully rendered only during the opt-in operator drill.

- [ ] **Step 3: Build the custom image without starting production**

    docker compose -f docker-compose.bot.yml build postgres
    docker image inspect --format '{{json .Config.Labels}}' bfx-postgres:local
    docker run --rm --entrypoint pgbackrest bfx-postgres:local version

Confirm the inspected labels contain the exact base digest, 2.59.1, and the exact tarball checksum. Do not run docker compose up, stanza-create, backup, restore, timer enablement, or any R2 command in the coding-agent verification pass.

- [ ] **Step 4: Record external acceptance items without fabricating evidence**

The operator, in the VM environment with real R2 credentials, must separately run smoke.sh --confirm-r2-smoke, verify list/head/read/write/multipart behavior through pgBackRest, run full/diff/info/verify, and run restore-drill.sh. Verify delete through pgBackRest expire only in a disposable R2 bucket or prefix with explicit operator confirmation; never use that deletion check against the production repository. Only the resulting measured JSON may populate Halt 2 evidence. A green offline suite proves contracts and isolation boundaries; it does not prove R2 reachability or measured RPO/RTO.

- [ ] **Step 5: Inspect the final diff and request review**

    git diff origin/main...HEAD --check
    git diff origin/main...HEAD --stat
    git status --short --branch

Confirm no credential, bfx_pgdata destructive restore command, personal second-brain reference, or unrelated user change is included. Then use the requesting-code-review skill before merge or push.
