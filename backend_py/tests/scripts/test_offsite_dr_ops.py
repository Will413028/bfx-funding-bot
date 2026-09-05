"""Offline behavioral contracts for pgBackRest operator wrappers."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
PG_BACKREST_DIR = ROOT / "deploy/vm/pgbackrest"
SECRET_VALIDATION_PATH = PG_BACKREST_DIR / "secret_validation.py"
TOKEN_SENTINEL = "TOKEN-SENTINEL"
SYSTEMD_DIR = ROOT / "deploy/vm/systemd"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _secret_text(**replacements: str) -> str:
    values = {
        "repo1-s3-endpoint": "https://account.r2.cloudflarestorage.com",
        "repo1-s3-bucket": "offsite-dr",
        "repo1-s3-key": "opaque-access-key",
        "repo1-s3-key-secret": TOKEN_SENTINEL,
        "repo1-cipher-pass": "opaque-cipher-pass",
        **replacements,
    }
    assignments = "\n".join(f"{key}={value}" for key, value in values.items())
    return f"# VM-only values\n\n[global]\n{assignments}\n"


def _write_secret_dir(
    tmp_path: Path,
    *,
    text: str | None = None,
    file_mode: int = 0o600,
    directory_mode: int = 0o700,
) -> tuple[Path, Path]:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    os.chown(secret_dir, -1, os.getgid())
    secret_file = secret_dir / "r2.conf"
    secret_file.write_text(text if text is not None else _secret_text(), encoding="utf-8")
    secret_file.chmod(file_mode)
    secret_dir.chmod(directory_mode)
    return secret_dir, secret_file


def _secret_validation() -> ModuleType:
    return _load_module("offsite_dr_secret_validation", SECRET_VALIDATION_PATH)


def test_secret_validator_accepts_exact_options_without_returning_values(tmp_path: Path) -> None:
    secret_dir, secret_file = _write_secret_dir(tmp_path)
    validator = _secret_validation()

    result = validator.validate_secret_dir(
        secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
    )

    assert result == (secret_file,)
    assert all(isinstance(path, Path) for path in result)
    assert TOKEN_SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "invalid_text",
    (
        _secret_text() + "repo1-s3-key=duplicate\n",
        _secret_text().replace(
            "repo1-s3-endpoint=https://account.r2.cloudflarestorage.com\n", ""
        ),
        _secret_text() + "repo1-s3-region=auto\n",
        _secret_text(**{"repo1-s3-key": ""}),
        _secret_text(**{"repo1-s3-endpoint": "https://<ACCOUNT_ID>.invalid"}),
        _secret_text(**{"repo1-s3-bucket": "Production-Example"}),
    ),
    ids=("duplicate", "missing", "unknown", "empty", "angle-marker", "example-marker"),
)
def test_secret_validator_rejects_invalid_assignments_without_leaking(
    tmp_path: Path, invalid_text: str
) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, text=invalid_text)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError) as caught:
        validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
        )

    assert str(caught.value) == "secret_config_invalid"
    assert TOKEN_SENTINEL not in str(caught.value)


def test_secret_validator_rejects_symlink_file_without_leaking(tmp_path: Path) -> None:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    secret_dir.chmod(0o700)
    target = tmp_path / "r2-target.conf"
    target.write_text(_secret_text(), encoding="utf-8")
    target.chmod(0o600)
    (secret_dir / "r2.conf").symlink_to(target)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError) as caught:
        validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
        )

    assert str(caught.value) == "secret_config_invalid"
    assert TOKEN_SENTINEL not in str(caught.value)


def test_secret_validator_rejects_group_read_for_a_non_postgres_group(
    tmp_path: Path,
) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, file_mode=0o640)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid() + 1
        )


@pytest.mark.parametrize("file_mode", (0o604, 0o620, 0o602))
def test_secret_validator_rejects_other_read_or_group_other_write(
    tmp_path: Path, file_mode: int
) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, file_mode=file_mode)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
        )


def test_secret_validator_rejects_directory_not_traversable_by_postgres(
    tmp_path: Path,
) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=70, postgres_gid=70)


def test_secret_validator_allows_postgres_group_read_and_traverse(tmp_path: Path) -> None:
    secret_dir, secret_file = _write_secret_dir(
        tmp_path, file_mode=0o640, directory_mode=0o750
    )
    validator = _secret_validation()

    result = validator.validate_secret_dir(
        secret_dir, postgres_uid=-1, postgres_gid=os.getgid()
    )

    assert result == (secret_file,)


@pytest.mark.parametrize("invalid", (False, True), ids=("valid", "placeholder"))
def test_secret_validator_cli_prints_only_bounded_error(tmp_path: Path, invalid: bool) -> None:
    secret_dir, _ = _write_secret_dir(
        tmp_path,
        text=_secret_text(
            **{"repo1-s3-key-secret": f"{TOKEN_SENTINEL}-example" if invalid else TOKEN_SENTINEL}
        ),
    )

    # Use this host's identity so the CLI reaches parsing on non-VM test hosts.
    entrypoint = (
        "import functools, os, sys; "
        "sys.path.insert(0, sys.argv.pop(1)); "
        "import secret_validation as validator; "
        "validator.validate_secret_dir = functools.partial(validator.validate_secret_dir, "
        "postgres_uid=os.getuid(), postgres_gid=os.getgid()); "
        "raise SystemExit(validator.main())"
    )
    completed = subprocess.run(
        [sys.executable, "-c", entrypoint, str(PG_BACKREST_DIR), "--secret-dir", str(secret_dir)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == (2 if invalid else 0)
    assert completed.stdout == ""
    assert completed.stderr == ("secret_config_invalid\n" if invalid else "")
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr


@pytest.mark.parametrize("marker", ("PLACEHOLDER", "ChangeMe", "change-me", "CHANGE_ME", "change me", "ReplaceMe", "replace-me", "replace_me", "replace me"))
def test_secret_validator_rejects_every_marker_variant(tmp_path: Path, marker: str) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, text=_secret_text(**{"repo1-cipher-pass": marker}))
    validator = _secret_validation()
    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())


def test_secret_validator_treats_values_as_opaque(tmp_path: Path) -> None:
    secret_dir, secret_file = _write_secret_dir(
        tmp_path,
        text=_secret_text(**{"repo1-cipher-pass": "opaque=a#b;c%value $d 'quoted'"}),
    )
    validator = _secret_validation()
    assert validator.validate_secret_dir(
        secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
    ) == (secret_file,)


@pytest.mark.parametrize("duplicate", (False, True))
def test_secret_validator_checks_all_direct_files_and_returns_sorted_paths(
    tmp_path: Path, duplicate: bool,
) -> None:
    secret_dir, secret_file = _write_secret_dir(
        tmp_path, text=_secret_text().replace("repo1-s3-key=opaque-access-key\n", "")
    )
    first = secret_dir / "a.conf"
    first.write_text("[global]\nrepo1-s3-key=opaque-access-key\n", encoding="utf-8")
    first.chmod(0o600)
    if duplicate:
        secret_file.write_text(_secret_text(), encoding="utf-8")
    validator = _secret_validation()
    if duplicate:
        with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
            validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())
    else:
        assert validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
        ) == (first, secret_file)


@pytest.mark.parametrize("kind", ("missing", "empty", "nested-directory", "fifo", "symlink-directory", "invalid-utf8", "malformed-line"))
def test_secret_validator_rejects_invalid_filesystem_or_text(tmp_path: Path, kind: str) -> None:
    secret_dir, secret_file = _write_secret_dir(tmp_path)
    if kind == "missing":
        secret_dir = tmp_path / "missing"
    elif kind == "empty":
        secret_file.unlink()
    elif kind == "nested-directory":
        (secret_dir / "nested").mkdir()
    elif kind == "fifo":
        os.mkfifo(secret_dir / "pipe")
    elif kind == "symlink-directory":
        link = tmp_path / "link"
        link.symlink_to(secret_dir, target_is_directory=True)
        secret_dir = link
    elif kind == "invalid-utf8":
        secret_file.write_bytes(b"\xff")
    else:
        secret_file.write_text(_secret_text() + "malformed\n", encoding="utf-8")
    validator = _secret_validation()
    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())


@pytest.mark.parametrize(
    ("directory_mode", "file_mode", "same_owner", "same_group"),
    ((0o755, 0o600, True, True), (0o720, 0o600, True, True),
     (0o702, 0o600, True, True), (0o600, 0o600, True, True),
     (0o740, 0o640, False, True), (0o710, 0o600, True, False),
     (0o750, 0o200, True, True), (0o750, 0o640, False, False),
     (0o750, 0o040, True, True)),
)
def test_secret_validator_rejects_unsafe_directory_or_unreadable_file(
    tmp_path: Path, directory_mode: int, file_mode: int, same_owner: bool, same_group: bool,
) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, directory_mode=directory_mode, file_mode=file_mode)
    try:
        validator = _secret_validation()
        with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
            validator.validate_secret_dir(
                secret_dir,
                postgres_uid=os.getuid() if same_owner else os.getuid() + 1,
                postgres_gid=os.getgid() if same_group else os.getgid() + 1,
            )
    finally:
        # Restore traversal for pytest's temporary-directory cleanup on macOS.
        secret_dir.chmod(0o700)
        (secret_dir / "r2.conf").chmod(0o600)


def test_pgbackrest_wrappers_fail_closed_and_have_no_mutating_sql() -> None:
    for name in ("status.sh", "preflight.sh", "backup.sh", "smoke.sh"):
        source = (PG_BACKREST_DIR / name).read_text(encoding="utf-8")
        assert "set -euo pipefail" in source
        assert "docker exec --user postgres" in source
        assert 'docker exec "$CONTAINER"' not in source
        assert not re.search(
            r"\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b",
            source,
            re.I,
        )
    assert "--confirm-r2-smoke" in (PG_BACKREST_DIR / "smoke.sh").read_text(
        encoding="utf-8"
    )


def test_all_vm_psql_docker_execs_run_as_postgres() -> None:
    offenders: list[str] = []
    matched = 0
    for script in sorted((ROOT / "deploy/vm").rglob("*.sh")):
        source = script.read_text(encoding="utf-8")
        logical_source = re.sub(r"\\\n[ \t]*", " ", source)
        for command in logical_source.splitlines():
            if "docker exec" not in command or re.search(r"\bpsql\b", command) is None:
                continue
            matched += 1
            if re.search(r"\bdocker exec\s+--user postgres(?:\s|$)", command) is None:
                offenders.append(str(script.relative_to(ROOT)))

    assert matched > 0
    assert offenders == []


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
    output.write_text(
        json.dumps({"schema_version": 1, "measured": True, "rpo_seconds": 1}),
        encoding="utf-8",
    )
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


@pytest.mark.parametrize("wrapper_name", ("preflight.sh", "backup.sh"))
def test_wrapper_failure_replaces_stale_green_evidence(
    tmp_path: Path, wrapper_name: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' 'TOKEN-SENTINEL' >&2
exit 23
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    output = tmp_path / "bfx/dr-evidence/backup.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({"schema_version": 1, "measured": True, "rpo_seconds": 1}),
        encoding="utf-8",
    )
    command = [str(PG_BACKREST_DIR / wrapper_name)]
    if wrapper_name == "preflight.sh":
        command.extend(("--output", str(output)))
    else:
        command.extend(("--type", "full"))

    completed = subprocess.run(
        command,
        cwd=ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BFX_POSTGRES_CONTAINER": "bfx-postgres-test",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 23
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "pgbackrest_check_failed"
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
        'git -C "$ROOT" ls-files --error-unmatch',
    ):
        assert required in source
    assert "BFX_VAULT_KEK" in source


def test_deploy_preflight_is_ordered_and_creates_only_non_secret_runtime_dirs() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    environment_checks = source.index(
        "set -a; . ./.env.frontend.runtime; set +a"
    )
    config_check = source.index('[ -r "$PGBACKREST_CONFIG" ]')
    tracked_config_check = source.index(
        'git -C "$ROOT" ls-files --error-unmatch'
    )
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
        < tracked_config_check
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


def test_deploy_preflight_fails_closed_for_untracked_or_modified_config() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    assert (
        'git -C "$ROOT" ls-files --error-unmatch -- "$PGBACKREST_CONFIG_REL"'
        in source
    )
    assert 'git -C "$ROOT" diff --quiet -- "$PGBACKREST_CONFIG_REL"' in source
    assert (
        'git -C "$ROOT" diff --cached --quiet -- "$PGBACKREST_CONFIG_REL"'
        in source
    )
    assert "pgBackRest config must be a clean tracked artifact" in source


def test_deploy_preflight_uses_shared_secret_validator_without_leaking() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    assert (
        'python3 "$ROOT/deploy/vm/pgbackrest/secret_validation.py" --secret-dir '
        '"$PGBACKREST_SECRET_DIR"'
    ) in source.replace("\\\n", "")
    assert "git grep -q -E" in source
    assert "':!docs/superpowers/specs/**'" in source
    for option in ("endpoint", "bucket", "key", "key-secret", "cipher-pass"):
        assert option in source
    assert 'find "$PGBACKREST_SECRET_DIR"' not in source
    assert 'cat "$secret_file"' not in source
    assert 'echo "$secret_value"' not in source


def test_deploy_tracked_secret_scan_fails_closed_on_command_errors() -> None:
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    assert "unable to scan tracked pgBackRest options" in source
    assert "PGBACKREST_TRACKED_SECRET_STATUS=$?" in source
    assert 'if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -eq 0 ]' in source
    assert 'if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -ne 1 ]' in source


@pytest.mark.parametrize("option", ("repo1-s3-endpoint", "repo1-s3-bucket", "repo1-s3-key", "repo1-s3-key-secret", "repo1-cipher-pass"))
@pytest.mark.parametrize("value", (TOKEN_SENTINEL, "", " \t"), ids=("nonempty", "empty", "whitespace"))
def test_deploy_tracked_secret_scan_checks_only_nonempty_assignments(
    tmp_path: Path, option: str, value: str,
) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True, capture_output=True)
    (tmp_path / "tracked.conf").write_text(f"  {option} = {value}\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "tracked.conf"), check=True, capture_output=True)
    source = (ROOT / "scripts/deploy-vm.sh").read_text(encoding="utf-8")
    scan = "PGBACKREST_SECRET_OPTION_PATTERN=" + source.split("PGBACKREST_SECRET_OPTION_PATTERN=", 1)[1]
    scan = scan.split("# The bind-mounted spool/log", 1)[0]

    completed = subprocess.run(
        ("bash", "-c", "set -eu\n" + scan), cwd=tmp_path, capture_output=True, text=True,
    )

    assert completed.returncode == (1 if value.strip() else 0)
    assert completed.stdout == ""
    assert completed.stderr == ("ERROR: secret pgBackRest option is tracked\n" if value.strip() else "")
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr


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
