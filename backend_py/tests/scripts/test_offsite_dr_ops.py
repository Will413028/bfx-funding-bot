"""Offline behavioral contracts for pgBackRest operator wrappers."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PG_BACKREST_DIR = ROOT / "deploy/vm/pgbackrest"
TOKEN_SENTINEL = "TOKEN-SENTINEL"
SYSTEMD_DIR = ROOT / "deploy/vm/systemd"


def test_pgbackrest_wrappers_fail_closed_and_have_no_mutating_sql() -> None:
    for name in ("status.sh", "preflight.sh", "backup.sh", "smoke.sh"):
        source = (PG_BACKREST_DIR / name).read_text(encoding="utf-8")
        assert "set -euo pipefail" in source
        assert not re.search(
            r"\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b",
            source,
            re.I,
        )
    assert "--confirm-r2-smoke" in (PG_BACKREST_DIR / "smoke.sh").read_text(
        encoding="utf-8"
    )


def test_status_is_read_only_and_does_not_restore_or_archive_push() -> None:
    source = (PG_BACKREST_DIR / "status.sh").read_text(encoding="utf-8")
    assert "pg_stat_archiver" in source
    assert "info --output=json" in source
    assert "--require-rpo" in source
    assert "archive-push" not in source
    assert "restore" not in source


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
printf '%s\\n' 'TOKEN-SENTINEL' >&2
case "$*" in
  *psql*)
    [[ "$*" == *'POSTGRES_USER'* && "$*" == *'POSTGRES_DB'* ]] || exit 42
    printf '%s\\n' "$FAKE_ARCHIVER_TSV"
    ;;
  *'pgbackrest --stanza=bfx info --output=json'*) printf '%s\\n' "$FAKE_INFO_JSON" ;;
  *) exit 41 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, log_path


def _info_json() -> str:
    return json.dumps(
        [
            {
                "name": "bfx",
                "backup": [
                    {
                        "label": "20260903031700-F",
                        "type": "full",
                        "timestamp": {
                            "start": {"epoch": 1756875000},
                            "stop": {"epoch": 1756875120},
                        },
                    },
                    {
                        "label": "20260904031700-D",
                        "type": "diff",
                        "timestamp": {
                            "start": {"epoch": 1756961220},
                            "stop": {"epoch": 1756961240},
                        },
                    },
                ],
            }
        ]
    )


def _run_status(tmp_path: Path, archiver_tsv: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir, log_path = _fake_docker(tmp_path)
    output = tmp_path / "backup.json"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_ARCHIVER_TSV": archiver_tsv,
        "FAKE_INFO_JSON": _info_json(),
        "BFX_POSTGRES_CONTAINER": "bfx-postgres-test",
    }
    completed = subprocess.run(
        [str(PG_BACKREST_DIR / "status.sh"), "--output", str(output), "--require-rpo"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, output, log_path


def test_status_collects_one_select_and_redacts_command_diagnostics(tmp_path: Path) -> None:
    completed, output, log_path = _run_status(
        tmp_path,
        "1756961300000\t1756961240000\t00000001000000000000000A\t0\t",
    )

    assert completed.returncode == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["rpo_seconds"] == 60
    docker_log = log_path.read_text(encoding="utf-8")
    assert len(re.findall(r"\bSELECT\b", docker_log, re.I)) == 1
    assert docker_log.count("pgbackrest --stanza=bfx info --output=json") == 1
    assert TOKEN_SENTINEL not in completed.stdout
    assert TOKEN_SENTINEL not in completed.stderr
    assert TOKEN_SENTINEL not in json.dumps(report)


def test_status_returns_nonzero_and_measured_false_for_stale_archive(tmp_path: Path) -> None:
    completed, output, _ = _run_status(
        tmp_path,
        "1756961300000\t1756960999000\t00000001000000000000000A\t0\t",
    )

    assert completed.returncode != 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "archive_lag_exceeded"
    assert "rpo_seconds" not in report
    assert TOKEN_SENTINEL not in completed.stdout
    assert TOKEN_SENTINEL not in completed.stderr
    assert TOKEN_SENTINEL not in json.dumps(report)


def test_pgbackrest_timers_have_the_intended_utc_schedule() -> None:
    backup_timer = (SYSTEMD_DIR / "bfx-pgbackrest-backup.timer").read_text(
        encoding="utf-8"
    )
    status_timer = (SYSTEMD_DIR / "bfx-pgbackrest-status.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 03:17:00 UTC" in backup_timer
    assert "OnCalendar=*:0/5" in status_timer
    assert "Persistent=true" in backup_timer
    assert "Persistent=true" in status_timer
    assert "WantedBy=timers.target" in backup_timer
    assert "WantedBy=timers.target" in status_timer


def test_services_are_one_shot_and_do_not_call_compose_run_or_autoheal() -> None:
    backup_service = (SYSTEMD_DIR / "bfx-pgbackrest-backup.service").read_text(
        encoding="utf-8"
    )
    status_service = (SYSTEMD_DIR / "bfx-pgbackrest-status.service").read_text(
        encoding="utf-8"
    )
    for service in (backup_service, status_service):
        assert "Requires=docker.service" in service
        assert "After=docker.service" in service
        assert "Type=oneshot" in service
        assert "User=ubuntu" in service
        assert "WorkingDirectory=/home/ubuntu/bfx-funding-bot" in service
        assert re.search(r"(?m)^TimeoutStartSec=[1-9][0-9]*$", service)
    assert (
        "ExecStart=/home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest/backup.sh "
        "--scheduled"
    ) in backup_service
    assert (
        "ExecStart=/home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest/status.sh "
        "--output /home/ubuntu/bfx/dr-evidence/backup.json"
    ) in status_service
    combined = backup_service + status_service
    assert "docker compose run" not in combined
    assert "autoheal" not in combined.lower()
    for forbidden in ("restore", "stanza-create", "expire", "resume", "halt"):
        assert forbidden not in combined.lower()


def test_deploy_preflight_checks_secret_boundary_and_custom_image_labels() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    for required in (
        "deploy/vm/pgbackrest/pgbackrest.conf",
        "pgbackrest/conf.d",
        "bfx-postgres:local",
        "org.opencontainers.image",
        "docker compose -f docker-compose.bot.yml config --quiet",
    ):
        assert required in source
    assert "BFX_VAULT_KEK" in source


def test_deploy_preflight_is_ordered_and_creates_only_non_secret_runtime_dirs() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    environment_checks = source.index(
        "set -a; . ./.env.frontend.runtime; set +a"
    )
    config_check = source.index('[ -r "$PGBACKREST_CONFIG" ]')
    runtime_install = source.index("install -d", config_check)
    compose_parse = source.index(
        "docker compose -f docker-compose.bot.yml config --quiet"
    )
    compose_build = source.index(
        "docker compose -f docker-compose.bot.yml build --build-arg"
    )
    image_inspect = source.index("docker image inspect", compose_build)
    compose_up = source.index("docker compose -f docker-compose.bot.yml up", image_inspect)
    assert (
        environment_checks
        < config_check
        < runtime_install
        < compose_parse
        < compose_build
        < image_inspect
        < compose_up
    )

    for runtime_dir in (
        '"$HOME/bfx/pgbackrest/spool"',
        '"$HOME/bfx/pgbackrest/log"',
        '"$HOME/bfx/dr-evidence"',
    ):
        assert runtime_dir in source
    assert 'install -d "$PGBACKREST_SECRET_DIR"' not in source
    assert 'mkdir -p "$PGBACKREST_SECRET_DIR"' not in source


def test_deploy_preflight_rejects_unsafe_or_placeholder_secret_files_without_leaking() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    assert 'find "$PGBACKREST_SECRET_DIR" -type f -perm -0007' in source
    assert 'find "$PGBACKREST_SECRET_DIR" -type f -exec sh -ceu' in source
    assert '"$PGBACKREST_EXAMPLE_PATTERN" {} +' in source
    assert "empty pgBackRest secret file" in source
    assert "example marker" in source
    assert "git grep -q -E" in source
    assert "':!docs/superpowers/specs/**'" in source
    assert 'cat "$secret_file"' not in source
    assert 'echo "$secret_value"' not in source


def test_deploy_secret_scans_fail_closed_on_command_errors() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    for error in (
        "unable to inspect pgBackRest secret file permissions",
        "unable to list pgBackRest secret files",
        "unable to scan tracked pgBackRest options",
    ):
        assert error in source
    assert "PGBACKREST_TRACKED_SECRET_STATUS=$?" in source
    assert 'if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -eq 0 ]' in source
    assert 'if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -ne 1 ]' in source


def test_deploy_requires_exact_task_1_image_labels_after_build() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    for expected in (
        "org.bfx.postgresql.base-digest",
        "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
        "org.bfx.pgbackrest.version",
        "2.59.1",
        "org.bfx.pgbackrest.source-sha256",
        "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
    ):
        assert expected in source
    assert "docker exec" not in source
    for forbidden_script in (
        "backup.sh",
        "status.sh",
        "smoke.sh",
        "restore-drill.sh",
    ):
        assert forbidden_script not in source
