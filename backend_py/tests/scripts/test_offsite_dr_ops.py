"""Offline behavioral contracts for pgBackRest operator wrappers."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
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


@pytest.mark.parametrize("suffix", [".txt", ".CONF", "", ".conf.bak"])
def test_secret_validator_rejects_files_pgbackrest_will_not_load(tmp_path: Path, suffix: str) -> None:
    secret_dir, secret_file = _write_secret_dir(tmp_path)
    secret_file.rename(secret_dir / ("r2" + suffix))
    validator = _secret_validation()
    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())


@pytest.mark.parametrize("header", ["", "[bfx]", "[global:backup]", "[global]\n[global]",
                                   "[global]\n[bfx]", "[DEFAULT]", "[ global ]"])
def test_secret_validator_rejects_wrong_or_ambiguous_section(tmp_path: Path, header: str) -> None:
    secret_dir, _ = _write_secret_dir(tmp_path, text=_secret_text().replace("[global]", header))
    validator = _secret_validation()
    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())


@pytest.mark.parametrize("control", ["\x00", "\v", "\f", "\x85", "\u2028"])
def test_secret_validator_rejects_ambiguous_control_characters(tmp_path: Path, control: str) -> None:
    secret_dir, _ = _write_secret_dir(
        tmp_path, text=_secret_text(**{"repo1-cipher-pass": f"opaque{control}#hidden"}),
    )
    validator = _secret_validation()
    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid())


@pytest.mark.parametrize(
    "line_ending",
    (b"\n", b"\r\n"),
    ids=("lf", "crlf"),
)
def test_secret_validator_accepts_pgbackrest_line_endings_without_returning_values(
    tmp_path: Path, line_ending: bytes,
) -> None:
    secret_dir, secret_file = _write_secret_dir(tmp_path)
    secret_file.write_bytes(_secret_text().encode("utf-8").replace(b"\n", line_ending))
    validator = _secret_validation()

    result = validator.validate_secret_dir(
        secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
    )

    assert result == (secret_file,)
    assert all(isinstance(path, Path) for path in result)
    assert TOKEN_SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "invalid_bytes",
    (
        _secret_text().encode("utf-8").replace(b"\n", b"\r"),
        _secret_text().replace("# VM-only values", "; not-a-pgbackrest-comment").encode(),
    ),
    ids=("bare-cr", "semicolon-comment"),
)
def test_secret_validator_rejects_pgbackrest_incompatible_lines(
    tmp_path: Path, invalid_bytes: bytes,
) -> None:
    secret_dir, secret_file = _write_secret_dir(tmp_path)
    secret_file.write_bytes(invalid_bytes)
    validator = _secret_validation()

    with pytest.raises(validator.SecretConfigError, match=r"^secret_config_invalid$"):
        validator.validate_secret_dir(
            secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()
        )


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


@pytest.mark.parametrize(
    "arguments",
    (
        (),
        (TOKEN_SENTINEL,),
        ("--secret-dir",),
        ("--secret-dir", f"--{TOKEN_SENTINEL}"),
        ("--secret-dir", ".", f"--{TOKEN_SENTINEL}"),
        ("--secret-dir", ".", TOKEN_SENTINEL),
    ),
    ids=(
        "missing-required-option",
        "missing-required-option-with-token",
        "missing-option-value",
        "missing-option-value-with-token",
        "unknown-option-with-token",
        "unknown-positional-with-token",
    ),
)
def test_secret_validator_cli_rejects_malformed_arguments_without_echo(
    arguments: tuple[str, ...],
) -> None:
    completed = subprocess.run(
        [sys.executable, str(SECRET_VALIDATION_PATH), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "secret_config_invalid\n"
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


@pytest.mark.parametrize("invalid", ["valid", "stanza", "repo", "status", "repo-status", "bool-status", "malformed"])
def test_smoke_validates_private_info_without_raw_output(tmp_path: Path, invalid: str) -> None:
    payload = json.loads(_info_json())
    if invalid == "stanza":
        payload[0]["name"] = "other"
    elif invalid == "repo":
        payload[0]["repo"][0]["key"] = 2
    elif invalid == "status":
        payload[0]["status"]["code"] = 99
    elif invalid == "repo-status":
        payload[0]["repo"][0]["status"]["code"] = 99
    elif invalid == "bool-status":
        payload[0]["status"]["code"] = False
    payload[0]["status"]["message"] = TOKEN_SENTINEL
    raw = json.dumps(payload) if invalid != "malformed" else TOKEN_SENTINEL
    completed, calls, leftovers = _smoke(tmp_path, raw)
    assert completed.returncode == (0 if invalid == "valid" else 2)
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr
    assert not leftovers
    if invalid != "valid":
        assert "verify" not in calls


@pytest.mark.parametrize("stage", ["stanza-create", "check", "--type=full", "--type=diff", "info", "verify"])
def test_smoke_command_failures_are_bounded_and_trap_cleaned(tmp_path: Path, stage: str) -> None:
    completed, _, leftovers = _smoke(tmp_path, _info_json(), failed_stage=stage)
    assert completed.returncode == 2
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr
    assert not leftovers


def _smoke(tmp_path: Path, raw: str, *, failed_stage: str = ""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    log = tmp_path / "calls"
    docker = bin_dir / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
printf '%s\\n' 'TOKEN-SENTINEL' >&2
if [[ -n "$FAILED_STAGE" && "$*" == *"$FAILED_STAGE"* ]]; then
  printf '%s\\n' 'TOKEN-SENTINEL'
  exit 23
fi
if [[ "$*" == *" info "* ]]; then
  printf '%s\\n' "$FAKE_INFO_JSON"
else
  printf '%s\\n' 'TOKEN-SENTINEL'
fi
""")
    docker.chmod(0o755)
    completed = subprocess.run(
        [str(PG_BACKREST_DIR / "smoke.sh"), "--confirm-r2-smoke"], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "TMPDIR": str(temporary), "FAKE_DOCKER_LOG": str(log),
             "FAKE_INFO_JSON": raw, "FAILED_STAGE": failed_stage},
    )
    return completed, log.read_text(), list(temporary.iterdir())


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
    [[ "$*" == *'-U bfx'* && "$*" == *'-d bfx'* ]] || exit 42
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
                "status": {"code": 0, "message": "ok"},
                "repo": [{"key": 1, "cipher": "aes-256-cbc", "status": {"code": 0, "message": "ok"}}],
                "backup": [
                    {
                        "label": "20260903031700-F",
                        "type": "full",
                        "timestamp": {
                            "start": 1756875000,
                            "stop": 1756875120,
                        },
                    },
                    {
                        "label": "20260904031700-D",
                        "type": "diff",
                        "timestamp": {
                            "start": 1756961220,
                            "stop": 1756961240,
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


def test_status_freshness_is_enforced_by_halt2_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.halt2_cutover import _read_dr_measurement

    completed, output, _ = _run_status(
        tmp_path, "1756961300000\t1756961240000\t00000001000000000000000A\t0\t",
    )
    assert completed.returncode == 0
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1756961300)
    assert _read_dr_measurement(output, key="rpo_seconds") == 60
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1756962201)
    with pytest.raises(ValueError, match="rpo_seconds_measurement_stale"):
        _read_dr_measurement(output, key="rpo_seconds")


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
    assert "exec --user postgres bfx-postgres-test psql" in docker_log
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


@pytest.mark.parametrize("wrapper", ["status.sh", "preflight.sh", "backup.sh"])
@pytest.mark.parametrize("fault", ["enospc", "mktemp"])
def test_wrapper_persistence_fault_revokes_old_measurement(
    tmp_path: Path, wrapper: str, fault: str,
) -> None:
    from scripts.halt2_cutover import _read_dr_measurement

    bin_dir, log = _fake_docker(tmp_path)
    output = tmp_path / "bfx/dr-evidence/backup.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"measured": True, "rpo_seconds": 1,
                                  "observed_at_ms": time.time_ns() // 1_000_000}))
    assert _read_dr_measurement(output, key="rpo_seconds") == 1
    shim = bin_dir / ("python3" if fault == "enospc" else "mktemp")
    shim.write_text(
        f"#!{sys.executable}\n"
        "import errno, runpy, sys, tempfile\n"
        "def fail(*args, **kwargs):\n    raise OSError(errno.ENOSPC, 'TOKEN-SENTINEL')\n"
        "tempfile.NamedTemporaryFile = fail\n"
        "target = sys.argv.pop(1)\nrunpy.run_path(target, run_name='__main__')\n"
        if fault == "enospc" else "#!/usr/bin/env bash\nexit 1\n"
    )
    shim.chmod(0o755)
    args = ["--type", "full"] if wrapper == "backup.sh" else ["--output", str(output)]
    completed = subprocess.run(
        [str(PG_BACKREST_DIR / wrapper), *args], capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path),
             "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "FAKE_DOCKER_LOG": str(log), "FAKE_INFO_JSON": _info_json(),
             "FAKE_ARCHIVER_TSV": "1756961300000\t1756961240000\twal\t0\t"},
    )
    assert completed.returncode != 0
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr
    with pytest.raises(ValueError):
        _read_dr_measurement(output, key="rpo_seconds")
    assert '"measured":true' not in completed.stdout.replace(" ", "")


def test_status_never_prints_stale_output_when_renderer_exits_nonzero(tmp_path: Path) -> None:
    bin_dir, log = _fake_docker(tmp_path)
    renderer = bin_dir / "python3"
    renderer.write_text("#!/usr/bin/env bash\nexit 2\n")
    renderer.chmod(0o755)
    output = tmp_path / "backup.json"
    output.write_text('{"measured": true, "rpo_seconds": 1}')
    completed = subprocess.run(
        [str(PG_BACKREST_DIR / "status.sh"), "--output", str(output)],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "FAKE_DOCKER_LOG": str(log), "FAKE_INFO_JSON": _info_json(), "FAKE_ARCHIVER_TSV": ""},
    )
    assert completed.returncode == 2
    assert completed.stdout == ""


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


def _run_deploy_through_secret_preflight(
    tmp_path: Path, secret_bytes: bytes,
) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    for directory in (
        repo / "scripts",
        repo / "deploy/vm/pgbackrest",
        home / "bfx/pgbackrest/conf.d",
        bin_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2(ROOT / "scripts/deploy-vm.sh", repo / "scripts/deploy-vm.sh")
    shutil.copy2(SECRET_VALIDATION_PATH, repo / "deploy/vm/pgbackrest/secret_validation.py")
    shutil.copy2(PG_BACKREST_DIR / "pgbackrest.conf", repo / "deploy/vm/pgbackrest/pgbackrest.conf")
    (repo / "deploy/vm/paper.env").write_text(
        "BFX_PHASE=paper\nBFX_DEPLOYMENT_ENV=paper\nBFX_EXECUTION_POLICY=paper\n",
        encoding="utf-8",
    )
    (home / "bfx/bot.env").write_text(
        "DATABASE_URL=postgresql://local/test\n"
        "BFX_EXCHANGE_ACCOUNT_ID=3f19d046-5030-494c-9a0a-9573bb890c1f\n"
        "BFX_VAULT_KEK=opaque\n",
        encoding="utf-8",
    )
    (home / "bfx/webapi.env").write_text(
        "DATABASE_URL=postgresql://local/test\n"
        "BETTER_AUTH_JWKS_URL=https://local.invalid/jwks\n"
        "BFX_VAULT_KEK=opaque\n"
        "BFX_OPERATOR_ROLE=admin\n"
        "BFX_OPERATOR_USER_ID=operator\n",
        encoding="utf-8",
    )
    (home / "bfx/frontend.env").write_text(
        "NEXT_PUBLIC_APP_URL=https://local.invalid\n"
        "NEXT_PUBLIC_BETTER_AUTH_URL=https://local.invalid\n"
        "API_URL=http://webapi\n"
        "BETTER_AUTH_SECRET=opaque\n"
        "BETTER_AUTH_URL=https://local.invalid\n"
        "DATABASE_URL=postgresql://local/test\n"
        "REDIS_URL=redis://local\n"
        "PASSKEY_RP_ID=local.invalid\n"
        "BFX_OPERATOR_USER_ID=operator\n"
        "BFX_OPERATOR_ROLE=admin\n",
        encoding="utf-8",
    )
    secret_dir = home / "bfx/pgbackrest/conf.d"
    secret_file = secret_dir / "r2.conf"
    secret_file.write_bytes(secret_bytes)
    secret_dir.chmod(0o700)
    secret_file.chmod(0o600)

    real_git = shutil.which("git")
    assert real_git is not None
    subprocess.run((real_git, "init", "-q", str(repo)), check=True)
    subprocess.run((real_git, "-C", str(repo), "add", "deploy/vm/pgbackrest/pgbackrest.conf"), check=True)
    subprocess.run(
        (
            real_git,
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@invalid",
            "commit",
            "-qm",
            "fixture",
        ),
        check=True,
    )
    git_shim = bin_dir / "git"
    git_shim.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == \"pull --ff-only origin main\" ]]; then exit 0; fi\n"
        f"exec {shlex.quote(real_git)} \"$@\"\n",
        encoding="utf-8",
    )
    git_shim.chmod(0o755)

    python_shim = bin_dir / "python3"
    python_shim.write_text(
        f"#!{sys.executable}\n"
        "import functools, os, pathlib, sys\n"
        f"real_python = {sys.executable!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1].endswith('/secret_validation.py'):\n"
        "    module_dir = str(pathlib.Path(sys.argv[1]).parent)\n"
        "    sys.path.insert(0, module_dir)\n"
        "    import secret_validation as validator\n"
        "    validator.validate_secret_dir = functools.partial(\n"
        "        validator.validate_secret_dir, postgres_uid=os.getuid(), postgres_gid=os.getgid()\n"
        "    )\n"
        "    status = validator.main(sys.argv[2:])\n"
        "    raise SystemExit(42 if status == 0 else status)\n"
        "os.execv(real_python, [real_python, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    python_shim.chmod(0o755)

    return subprocess.run(
        (str(repo / "scripts/deploy-vm.sh"), "paper"),
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )


@pytest.mark.parametrize(
    "invalid_bytes",
    (
        _secret_text().encode("utf-8").replace(b"\n", b"\r"),
        _secret_text().replace("# VM-only values", "; not-a-pgbackrest-comment").encode(),
    ),
    ids=("bare-cr", "semicolon-comment"),
)
def test_deploy_caller_rejects_pgbackrest_incompatible_secret_lines(
    tmp_path: Path, invalid_bytes: bytes,
) -> None:
    completed = _run_deploy_through_secret_preflight(tmp_path, invalid_bytes)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "secret_config_invalid\n"
    assert TOKEN_SENTINEL not in completed.stdout + completed.stderr


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


def _assert_ordered(text: str, markers: tuple[str, ...]) -> None:
    cursor = -1
    for marker in markers:
        position = text.find(marker, cursor + 1)
        assert position > cursor, marker
        cursor = position


def test_offsite_runbook_orders_install_acceptance_restore_and_timer_enablement() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "./scripts/install-pgbackrest-timers.sh" in text
    assert "sudo install -m 0644 deploy/vm/systemd/" not in text
    assert "terraform init -backend-config=backend.hcl" not in text
    assert "terraform plan -out=r2.tfplan" not in text
    assert "terraform apply r2.tfplan" not in text

    _assert_ordered(
        text,
        (
            "### 0. Provision the R2 backup bucket with Terraform",
            "cp infra/terraform/r2/backend.hcl.example /secure/path/backend.hcl",
            "cp infra/terraform/r2/terraform.tfvars.example /secure/path/terraform.tfvars",
            "cd infra/terraform/r2",
            "read -r -s -p 'Cloudflare management token (hidden; not saved to history): ' CLOUDFLARE_API_TOKEN",
            "export CLOUDFLARE_API_TOKEN",
            "terraform init -backend-config=/secure/path/backend.hcl",
            "terraform plan -var-file=/secure/path/terraform.tfvars -out=/secure/path/r2.tfplan",
            "terraform show /secure/path/r2.tfplan",
            "terraform apply /secure/path/r2.tfplan",
            "cd ../../..",
            "./scripts/install-pgbackrest-timers.sh",
            "./scripts/setup-pgbackrest-r2.sh",
            "Build and validate bfx-postgres:local",
            "`stanza-create`",
            "Run the staged isolated restore with --baseline",
            "Accept only fresh measured evidence",
            "Enable, start, and list the timers",
        ),
    )
    _assert_ordered(
        text,
        (
            "`stanza-create`",
            "`check`",
            "`--type=full backup`",
            "`--type=diff backup`",
            "`info --output=json`",
            "`verify`",
            "pgbackrest --stanza=bfx expire",
            "Capture the same-target bounded baseline.json",
            "Run the staged isolated restore with --baseline",
            "Accept only fresh measured evidence",
            "Enable, start, and list the timers",
            "Declare DR ready",
        ),
    )
    for marker in (
        "Terraform state bucket is separate from the pgBackRest backup bucket",
        "runtime R2 token is not a Terraform resource",
        "R2 lifecycle does not own pgBackRest retention",
        "coding agent does not perform production terraform apply or token creation",
    ):
        assert marker in normalized
    # Headings alone miss commands pasted above the gates. Check every actual
    # activation (including sudo/options/continued lines), not just the first.
    flattened = text.replace("\\\n", "  ")
    activation_gate = flattened.index("### 9. Enable, start, and list the timers")
    gate_markers = (
        "`check`",
        "`--type=full backup`",
        "`--type=diff backup`",
        "`info --output=json`",
        "`verify`",
        "pgbackrest --stanza=bfx expire",
        "preflight.sh --output",
        "load_restore_baseline(",
        "--baseline /absolute/path/baseline.json",
        "egress_disconnected: true",
        "Never edit\nJSON to manufacture acceptance.",
    )
    for marker in gate_markers:
        assert 0 <= flattened.index(marker) < activation_gate, marker
    activations = []
    for match in re.finditer(r"\bsystemctl\b[^\n`]*", flattened):
        argv = shlex.split(match.group())
        if {"enable", "start"}.intersection(argv) and any(
            arg.endswith(".timer") for arg in argv
        ):
            assert match.start() > activation_gate, "timer activation before acceptance gates"
            activations.append(argv)
    assert any("enable" in argv for argv in activations)
    assert any("start" in argv for argv in activations)
    assert "systemctl enable --now" not in text
    assert "sudo systemctl enable bfx-pgbackrest-backup.timer" in text
    assert "sudo systemctl start bfx-pgbackrest-backup.timer" in text
    assert "systemctl list-timers --all" in text


def test_offsite_terraform_docs_read_management_token_without_history_assignment() -> None:
    safe_prompt = (
        "read -r -s -p 'Cloudflare management token (hidden; not saved to history): ' "
        "CLOUDFLARE_API_TOKEN"
    )
    for path in (
        ROOT / "docs/runbooks/offsite-dr.md",
        ROOT / "infra/terraform/r2/README.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert safe_prompt in text
        assert "export CLOUDFLARE_API_TOKEN" in text
        assert "export CLOUDFLARE_API_TOKEN=" not in text


def test_offsite_runbook_exports_shared_secret_dir_before_preferred_wizard() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    assignment = text.index('PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"')
    export = text.index("export PGBACKREST_SECRET_DIR", assignment)
    wizard = text.index("./scripts/setup-pgbackrest-r2.sh")
    section_three = text.index("### 3. Build and validate bfx-postgres:local")
    mount = text.index('src=$PGBACKREST_SECRET_DIR', section_three)

    assert assignment < export < wizard < section_three < mount
    assert text.count('PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"') == 1


def test_offsite_runbook_documents_secret_and_archive_contracts() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")

    for option in (
        "repo1-s3-endpoint",
        "repo1-s3-bucket",
        "repo1-s3-key",
        "repo1-s3-key-secret",
        "repo1-cipher-pass",
    ):
        assert option in text
    for marker in (
        "exactly these five options",
        "UID/GID 70",
        "mode `0750`",
        "mode `0640`",
        'sudo chown "$(id -u):70"',
        'test -r "$PGBACKREST_SECRET_DIR/r2.conf"',
        '--user 70:70 --network none',
        'test -r /secrets/r2.conf',
        "secret_validation.py",
        "archive_timeout=60s",
        "docker exec --user postgres",
        "dedicated disposable R2 repository",
    ):
        assert marker in text


def test_offsite_runbook_disposable_expire_proves_inventory_deletion() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    for marker in (
        "repo1-retention-full=1",
        "repo1-retention-diff=1",
        "expire-auto=n",
        "for backup_type in full diff full diff",
        '"$DISPOSABLE_CONTAINER" != bfx-postgres',
        "before.json",
        "after.json",
        "assert len(full) == 2 and len(diff) == 2",
        "assert after == {full[-1], diff[-1]}",
        "assert set(before) - after == {full[0], diff[0]}",
    ):
        assert marker in text


def test_offsite_runbook_baseline_has_read_only_capture_and_offline_assembly() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    for marker in (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
        "exchange_account_id = :'account_id'::uuid",
        "deployment_environment = :'environment'",
        "FROM public.alembic_version",
        "ORDER BY event_seq",
        "canonical_event_hash(rows)",
        'type(capture["event_count"]) is int',
        'type(capture["event_head"]) is int',
        "load_restore_baseline(",
        "operator-supplied",
        "all database writers",
        "not an automatic production baseline generator",
    ):
        assert marker in text


def test_offsite_runbook_null_target_requires_quiescence_or_matching_pitr() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "or empty for backup end" not in normalized
    for marker in (
        "does not set a recovery cutoff",
        "available archive stream",
        "all database writers remain stopped until `restore-db` has completed recovery",
        "If writers must resume after baseline capture",
        "explicit `--target-time`",
        "same non-null `target_time`",
        "same database state",
    ):
        assert marker in normalized


def _runbook_python_snippet(prefix: str) -> str:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")
    block = text[text.index(prefix):]
    return block.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


@pytest.mark.parametrize("remaining", ("retained", "unchanged", "empty", "lost-diff"))
def test_runbook_inventory_assertions_reject_noop_and_retained_data_loss(
    tmp_path: Path, remaining: str,
) -> None:
    labels = {
        "20260904-000000F": "full",
        "20260904-000000F_20260904-000100D": "diff",
        "20260905-000000F": "full",
        "20260905-000000F_20260905-000100D": "diff",
    }
    retained = dict(list(labels.items())[2:])
    after = {
        "retained": retained, "unchanged": labels, "empty": {},
        "lost-diff": {"20260905-000000F": "full"},
    }[remaining]
    for name, inventory in (("before", labels), ("after", after)):
        (tmp_path / f"{name}.json").write_text(json.dumps([{
            "name": "bfx", "status": {"code": 0},
            "backup": [{"label": label, "type": kind} for label, kind in inventory.items()],
        }]))
    completed = subprocess.run(
        [sys.executable, "-", str(tmp_path)],
        input=_runbook_python_snippet('python3 - "$DISPOSABLE_EVIDENCE_DIR"'),
        capture_output=True, text=True, check=False,
    )
    assert (completed.returncode == 0) is (remaining == "retained")


@pytest.mark.parametrize("invalid", (None, "scope", "count", "head", "migrations"))
def test_runbook_baseline_assembly_uses_existing_canonical_and_loader_contract(
    tmp_path: Path, invalid: str | None,
) -> None:
    account = "00000000-0000-0000-0000-000000000001"
    capture = {
        "database_name": "bfx", "account_id": account, "environment": "prod",
        "migration_heads": ["abc123"], "event_count": 1, "event_head": 7,
        "events": [{
            "event_seq": 7, "account_id": "legacy-account",
            "exchange_account_id": account, "deployment_environment": "prod",
            "event_type": "fixture", "cid": None, "venue_offer_id": None,
            "venue_seq": None, "event_id": None, "schema_version": 2,
            "payload": {}, "occurred_at_ms": 1,
        }],
    }
    if invalid == "scope":
        capture["events"][0]["deployment_environment"] = "shadow"
    elif invalid == "count":
        capture["event_count"] = True
    elif invalid == "head":
        capture["event_head"] = 7.0
    elif invalid == "migrations":
        capture["migration_heads"] = []
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    completed = subprocess.run(
        [sys.executable, "-", str(tmp_path), "bfx", account, "prod",
         "20260905-000000F", "", "execution-state-v1"],
        input=_runbook_python_snippet('uv run python - "$BASELINE_WORK_DIR"'),
        cwd=ROOT / "backend_py", capture_output=True, text=True, check=False,
    )
    assert (completed.returncode == 0) is (invalid is None), completed.stderr
    output = tmp_path / "baseline.json"
    if invalid is None:
        from uuid import UUID

        from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
        from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

        row = EventLogRow(**{**capture["events"][0], "exchange_account_id": UUID(account)})
        baseline = json.loads(output.read_text())
        assert baseline["event_hash"] == canonical_event_hash([row])
        assert baseline["event_count"] == 1 and baseline["event_head"] == 7
        assert baseline["target_time"] is None
        assert output.stat().st_mode & 0o777 == 0o600
    else:
        assert not output.exists()


@pytest.mark.parametrize("verb", ("enable", "start", "enable --now", "--now enable"))
@pytest.mark.parametrize("gate", (4, 5, 6, 7, 8, 9))
def test_offsite_runbook_rejects_timer_command_moved_before_gates(
    monkeypatch: pytest.MonkeyPatch, verb: str, gate: int,
) -> None:
    path = ROOT / "docs/runbooks/offsite-dr.md"
    original = path.read_text(encoding="utf-8")
    original_verb = "start" if verb == "start" else "enable"
    command = (
        f"sudo systemctl {original_verb} bfx-pgbackrest-backup.timer bfx-pgbackrest-status.timer"
    )
    moved = command.replace(f"systemctl {original_verb}", f"systemctl {verb}")
    mutated = original.replace(command + "\n", "")
    position = mutated.index(f"### {gate}.")
    mutated = mutated[:position] + f"```bash\n{moved}\n```\n\n" + mutated[position:]
    read_text = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *args, **kwargs: mutated if self == path else read_text(self, *args, **kwargs),
    )

    with pytest.raises(AssertionError, match="timer activation before acceptance gates"):
        test_offsite_runbook_orders_install_acceptance_restore_and_timer_enablement()


def test_offsite_runbook_documents_same_target_baseline_and_staged_restore() -> None:
    text = (ROOT / "docs/runbooks/offsite-dr.md").read_text(encoding="utf-8")

    for field in (
        "target_backup_label",
        "target_time",
        "database_name",
        "account_id",
        "environment",
        "projector_version",
        "migration_heads",
        "event_count",
        "event_head",
        "event_hash",
    ):
        assert f"`{field}`" in text
    for marker in (
        "same backup/PITR target",
        "Missing or mismatched baseline",
        "`restore-data`",
        "restore-db alone has temporary R2 egress",
        "Only then disconnect egress",
        "existing restored database",
        "ephemeral verifier role",
        "verifier has no R2 or application secrets",
        "local PostgreSQL socket as OS user `postgres`",
    ):
        assert marker in text

    command = """deploy/vm/pgbackrest/restore-drill.sh \\
  --account-id <canonical-uuid> \\
  --environment prod \\
  --projector-version execution-state-v1 \\
  --backup-label <label> \\
  --baseline /absolute/path/baseline.json"""
    assert command in text
    assert "docker volume rm bfx_pgdata" not in text
    assert "docker compose down -v" not in text
    assert "no Bitfinex request" in text


def test_architecture_and_halt1_document_staged_dr_and_fresh_evidence() -> None:
    architecture = (ROOT / "backend_py/ARCHITECTURE.md").read_text(encoding="utf-8")
    halt1 = (ROOT / "docs/runbooks/halt-1-exchange-account-cutover.md").read_text(
        encoding="utf-8"
    )

    for marker in (
        "restore-data",
        "restore-db",
        "R2 egress",
        "ephemeral verifier role",
        "existing restored database",
        "observed_at_ms",
        "900 seconds",
        "venue rollback",
    ):
        assert marker in architecture
    assert "[Offsite DR operator runbook](offsite-dr.md)" in halt1
    assert "same backup/PITR target" in halt1
    assert "fresh measured evidence" in halt1


def test_halt1_legacy_backup_precedes_identity_and_uuid_gate_follows_cutover() -> None:
    halt1 = (ROOT / "docs/runbooks/halt-1-exchange-account-cutover.md").read_text()
    step2 = halt1.split("### 2.", 1)[1].split("### 3.", 1)[0]
    assert "legacy-schema-compatible" in step2
    assert "canonical UUID baseline" not in step2
    assert "cutover_identity.py" not in step2
    assert "#post-identity-dr-gate" in step2
    gate = halt1.split('<a id="post-identity-dr-gate"></a>', 1)[1]
    assert halt1.index('<a id="post-identity-dr-gate"></a>') > halt1.index("### 6a.")
    assert "canonical UUID baseline" in gate
    assert "same backup/PITR target" in gate
    assert "fresh measured evidence" in gate
    assert "offsite-dr.md#6-" in gate and "offsite-dr.md#7-" in gate
    assert "docker exec bfx-postgres psql" not in halt1


def test_runbook_broad_runtime_contracts_match_current_consumers() -> None:
    runbook = (ROOT / "docs/runbooks/offsite-dr.md").read_text()
    assert "--username bfx" in runbook
    assert "--username postgres" not in runbook
    for marker in ("pg1-user=bfx", "pg_is_in_recovery()", "SQL admin role",
                   "sanitized", "COMPOSE_*", "exactly the generated internal network",
                   "smoke_info_ok", "status.code", "ENOSPC", "verifier container",
                   "legacy-schema-compatible"):
        assert marker in runbook
