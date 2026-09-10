"""Static contracts for the VM-only offsite DR bootstrap scripts."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_projection_cutover_archive import (
    archive_db as archive_db,
)
from tests.integration.test_projection_cutover_archive import (
    archive_pg,  # noqa: F401
)

ROOT = Path(__file__).resolve().parents[3]
WIZARD_PATH = ROOT / "scripts/setup-pgbackrest-r2.sh"
INSTALLER_PATH = ROOT / "scripts/install-pgbackrest-timers.sh"
EXPECTED_UNITS = (
    "bfx-pgbackrest-backup.service",
    "bfx-pgbackrest-backup.timer",
    "bfx-pgbackrest-status.service",
    "bfx-pgbackrest-status.timer",
)
EXPECTED_TIMERS = (
    "bfx-pgbackrest-backup.timer",
    "bfx-pgbackrest-status.timer",
)

wizard_text = WIZARD_PATH.read_text(encoding="utf-8")
installer_text = INSTALLER_PATH.read_text(encoding="utf-8")


@pytest.mark.integration
async def test_real_generated_role_verifies_archive_with_select_only_and_missing_select_fails(archive_db):
    from uuid import uuid4

    import psycopg
    from sqlalchemy import event, text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scripts.verify_projection_archive import verify_archives
    from tests.integration.test_projection_cutover_archive import SCOPE, capture
    from tests.scripts.test_offsite_dr_restore import RestoreDrill, build_restore_plan

    factory, engine = archive_db
    expected = await capture(factory)
    plan = build_restore_plan(account_id=str(SCOPE.account_id), environment="ci",
        projector_version="execution-state-v1", backup_label="20260904031700-F", target_time=None,
        run_id="20260904T031700Z-" + uuid4().hex[:16], database_name=engine.url.database,
        expected_event_hash=expected.stream.digest)
    # psycopg expects the driver-neutral URL; command runner executes actual generated SQL.
    url = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    def runner(command, *, input_text=None, **kwargs):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(input_text, prepare=False)
        return subprocess.CompletedProcess(command, 0, "", "")
    drill = RestoreDrill(command_runner=runner)
    drill._deadline = drill._clock() + 60
    drill._bootstrap_role(plan, "synthetic-password-only")
    verifier = create_async_engine(engine.url.set(drivername="postgresql+asyncpg",
        username=plan.verify_role, password="synthetic-password-only"))
    try:
        async with async_sessionmaker(verifier)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            statements = []
            def observe(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement.strip())
            event.listen(verifier.sync_engine, "before_cursor_execute", observe)
            report = await verify_archives(session, scope=SCOPE, expected=(expected,))
            event.remove(verifier.sync_engine, "before_cursor_execute", observe)
            assert report[0]["verified_counts"]["position_state"] == 1
            assert statements and all(s.upper().startswith("SELECT") for s in statements)
            assert await session.scalar(text("SELECT has_table_privilege(current_user, 'projection_audit.rows', 'INSERT,UPDATE,DELETE')")) is False
            assert await session.scalar(text("SELECT has_schema_privilege(current_user, 'projection_audit', 'CREATE')")) is False
            assert await session.scalar(text("SELECT has_table_privilege(current_user, 'exchange_account_credentials', 'SELECT')")) is False
        with engine.begin() as conn:
            conn.exec_driver_sql(f'REVOKE SELECT ON projection_audit.rows FROM "{plan.verify_role}"')
        async with async_sessionmaker(verifier)() as session:
            with pytest.raises(DBAPIError):
                await verify_archives(session, scope=SCOPE, expected=(expected,))
    finally:
        await verifier.dispose()
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP OWNED BY "{plan.verify_role}"')
            conn.exec_driver_sql(f'DROP ROLE "{plan.verify_role}"')


def test_secret_wizard_captures_only_vm_secret_fragment() -> None:
    """The wizard stores only the approved fragment under the VM boundary."""
    stage_text = wizard_text.split("# STAGES", 1)[1]

    assert "# STAGES" in wizard_text
    assert 'ENV_FILE="$SECRET_FILE"' in stage_text
    assert "_prompt_candidate R2_SECRET_ACCESS_KEY repo1-s3-key-secret" in stage_text
    assert "_prompt_candidate R2_CIPHER_PASS repo1-cipher-pass" in stage_text
    for option in (
        "repo1-s3-endpoint",
        "repo1-s3-bucket",
        "repo1-s3-key",
        "repo1-s3-key-secret",
        "repo1-cipher-pass",
    ):
        assert option in stage_text
    assert 'python3 "$ROOT/deploy/vm/pgbackrest/secret_validation.py" --secret-dir "$CANDIDATE_DIR"' in stage_text
    assert "install-pgbackrest-timers.sh" in stage_text
    assert 'sudo chown "$(id -u):70" "$SECRET_DIR"' in stage_text
    assert 'sudo chown "$(id -u):70" "$CANDIDATE_DIR" "$CANDIDATE_FILE"' in stage_text
    assert 'sudo chmod 0750 "$SECRET_DIR"' in stage_text
    assert 'sudo chmod 0640 "$CANDIDATE_FILE"' in stage_text
    assert 'sudo chown "$(id -u):70" "$SECRET_DIR" "$SECRET_FILE"' in stage_text
    assert 'sudo chmod 0640 "$SECRET_FILE"' in stage_text
    assert 'python3 "$ROOT/deploy/vm/pgbackrest/secret_validation.py" --secret-dir "$SECRET_DIR"' in stage_text
    assert 'sudo chown 70:70 "$SECRET_DIR" "$SECRET_FILE"' not in stage_text
    assert "https://developers.cloudflare.com/r2/api/tokens/" in stage_text
    assert "set_secret " not in stage_text
    assert "BFX_API_SECRET" not in stage_text
    assert ".env" not in stage_text
    assert "bot.env" not in stage_text
    assert "webapi.env" not in stage_text
    assert "frontend.env" not in stage_text


def test_timer_installer_is_definition_only() -> None:
    """Installer loads definitions but preserves all existing timer state."""
    for unit_name in EXPECTED_UNITS:
        assert unit_name in installer_text
    assert "/etc/systemd/system" in installer_text
    assert "systemctl daemon-reload" in installer_text
    assert "systemctl is-active" in installer_text
    assert "systemctl is-enabled" in installer_text
    assert not re.search(
        r"\bsystemctl\s+(?:--\S+\s+)*(?:enable|start|stop|disable)\b",
        installer_text,
    )
    assert "systemctl stop" not in installer_text
    assert "systemctl disable" not in installer_text
    assert "systemctl enable" not in installer_text
    assert "systemctl start" not in installer_text
    assert "enable --now" not in installer_text
    assert "docker" not in installer_text.lower()
    assert '"pgbackrest"' not in installer_text.lower()


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _mock_systemctl() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\\n' "$*" >> "$MOCK_LOG"
case "${1:-}" in
  show)
    if [[ -f "$MOCK_RELOADED" ]]; then
      printf '%s\\n' loaded
    elif [[ "$MOCK_MODE" == query-error ]]; then
      exit 17
    elif [[ "$MOCK_MODE" == first-install ]]; then
      printf '%s\\n' not-found
    else
      printf '%s\\n' loaded
    fi
    ;;
  is-active)
    if [[ "$MOCK_MODE" == active && ! -f "$MOCK_RELOADED" ]]; then
      printf '%s\\n' active
      exit 0
    fi
    printf '%s\\n' inactive
    exit 3
    ;;
  is-enabled)
    if [[ "$MOCK_MODE" == enabled && ! -f "$MOCK_RELOADED" ]]; then
      printf '%s\\n' enabled
      exit 0
    fi
    printf '%s\\n' disabled
    exit 1
    ;;
  daemon-reload)
    : > "$MOCK_RELOADED"
    ;;
  *)
    exit 99
    ;;
esac
"""


def _timer_installer_fixture(tmp_path: Path, mode: str) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    destination = tmp_path / "systemd"
    bin_dir.mkdir()
    destination.mkdir()
    log = tmp_path / "calls.log"
    reloaded = tmp_path / "reloaded"
    for unit_name in EXPECTED_UNITS:
        (destination / unit_name).write_text(f"old-{unit_name}\n", encoding="utf-8")

    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\nprintf 'Linux\\n'\n")
    _write_executable(bin_dir / "systemctl", _mock_systemctl())
    _write_executable(
        bin_dir / "sudo",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'sudo %s\\n' "$*" >> "$MOCK_LOG"
case "$1" in
  install)
    shift
    args=("$@")
    source_path="${args[2]}"
    destination_path="${args[3]}"
    /usr/bin/install -m 0644 "$source_path" "$MOCK_DEST/${destination_path##*/}"
    ;;
  systemctl)
    shift
    exec "$MOCK_SYSTEMCTL" "$@"
    ;;
  *)
    exit 99
    ;;
esac
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MOCK_DEST": str(destination),
        "MOCK_LOG": str(log),
        "MOCK_MODE": mode,
        "MOCK_RELOADED": str(reloaded),
        "MOCK_SYSTEMCTL": str(bin_dir / "systemctl"),
    }
    return env, destination, log


@pytest.mark.parametrize("mode", ("active", "enabled", "query-error"))
def test_timer_installer_rejects_unsafe_state_before_overwriting_units(
    tmp_path: Path, mode: str,
) -> None:
    env, destination, log = _timer_installer_fixture(tmp_path, mode)

    completed = subprocess.run(
        (str(INSTALLER_PATH),), cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )

    assert completed.returncode != 0
    for unit_name in EXPECTED_UNITS:
        assert (destination / unit_name).read_text(encoding="utf-8") == f"old-{unit_name}\n"
    assert "daemon-reload" not in log.read_text(encoding="utf-8")


def test_timer_installer_first_install_verifies_safe_post_install_state(tmp_path: Path) -> None:
    env, destination, log = _timer_installer_fixture(tmp_path, "first-install")

    completed = subprocess.run(
        (str(INSTALLER_PATH),), cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    for unit_name in EXPECTED_UNITS:
        source = ROOT / "deploy/vm/systemd" / unit_name
        assert (destination / unit_name).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    calls = log.read_text(encoding="utf-8")
    assert "daemon-reload" in calls
    assert calls.count("sudo systemctl show --property=LoadState --value") == 2 * len(EXPECTED_TIMERS)


WIZARD_SECRET = """[global]
repo1-s3-endpoint=https://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.r2.cloudflarestorage.com
repo1-s3-bucket=offsite-dr
repo1-s3-key=access-key-old
repo1-s3-key-secret=secret-key-old
repo1-cipher-pass=cipher-pass-old
"""


def _wizard_fixture(
    tmp_path: Path, *, existing: str | None = None, validator_mode: str = "ok",
    target_kind: str = "file", extra_direct_file: bool = False,
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    destination = tmp_path / "systemd"
    secret_dir = tmp_path / "secrets"
    log = tmp_path / "calls.log"
    validator_log = tmp_path / "validator.log"
    reloaded = tmp_path / "reloaded"
    bin_dir.mkdir()
    destination.mkdir()
    _write_executable(bin_dir / "uname", "#!/usr/bin/env bash\nprintf 'Linux\\n'\n")
    _write_executable(bin_dir / "open", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "systemctl", _mock_systemctl())
    _write_executable(
        bin_dir / "sudo",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'sudo %s\\n' "$*" >> "$MOCK_LOG"
case "$1" in
  mkdir)
    shift
    /bin/mkdir "$@"
    ;;
  chown)
    ;;
  chmod)
    shift
    /bin/chmod "$@"
    ;;
  mktemp)
    shift
    exec /usr/bin/mktemp "$@"
    ;;
  tee)
    shift
    exec /usr/bin/tee "$@" >/dev/null
    ;;
  mv)
    shift
    exec /bin/mv "$@"
    ;;
  install)
    shift
    args=("$@")
    source_path="${args[2]}"
    destination_path="${args[3]}"
    /usr/bin/install -m 0644 "$source_path" "$MOCK_DEST/${destination_path##*/}"
    ;;
  systemctl)
    shift
    exec "$MOCK_SYSTEMCTL" "$@"
    ;;
  *)
    exit 99
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == */secret_validation.py ]]; then
  printf '%s\\n' "$*" >> "$MOCK_VALIDATOR_LOG"
  test "${2:-}" = --secret-dir
  test -f "${3:-}/r2.conf"
  [[ "$MOCK_VALIDATOR_MODE" == reject ]] && exit 2
  [[ "$MOCK_VALIDATOR_MODE" == interrupt ]] && exit 130
  exit 0
fi
exec /usr/bin/python3 "$@"
""",
    )
    if existing is not None:
        secret_dir.mkdir()
        secret_file = secret_dir / "r2.conf"
        if target_kind == "directory":
            secret_file.mkdir()
        else:
            secret_file.write_text(existing, encoding="utf-8")
        secret_dir.chmod(0o750)
        if secret_file.is_file():
            secret_file.chmod(0o640)
        if extra_direct_file:
            (secret_dir / "extra.conf").write_text("unexpected\n", encoding="utf-8")
    else:
        secret_file = secret_dir / "r2.conf"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PGBACKREST_SECRET_DIR": str(secret_dir),
        "MOCK_DEST": str(destination),
        "MOCK_LOG": str(log),
        "MOCK_MODE": "first-install",
        "MOCK_RELOADED": str(reloaded),
        "MOCK_SYSTEMCTL": str(bin_dir / "systemctl"),
        "MOCK_VALIDATOR_LOG": str(validator_log),
        "MOCK_VALIDATOR_MODE": validator_mode,
    }
    return env, secret_dir, secret_file, log, validator_log


def _run_wizard(env: dict[str, str], values: list[str]) -> subprocess.CompletedProcess[str]:
    input_text = "\n".join(["", *values]) + "\n"
    return subprocess.run(
        (str(WIZARD_PATH),), cwd=ROOT, env=env, input=input_text,
        capture_output=True, text=True, check=False,
    )


def _snapshot(path: Path) -> tuple[bytes, int, int, int, int]:
    metadata = path.stat()
    return (
        path.read_bytes(), metadata.st_mode & 0o777, metadata.st_uid,
        metadata.st_gid, metadata.st_mtime_ns,
    )


def test_wizard_runs_from_normal_checkout_and_sets_host_group_contract(tmp_path: Path) -> None:
    env, secret_dir, secret_file, log, validator_log = _wizard_fixture(tmp_path)
    values = ["a" * 32, "offsite-dr", "access-key", "secret-key-sentinel", "cipher-pass-sentinel"]

    completed = _run_wizard(env, values)

    assert completed.returncode == 0, completed.stderr
    assert secret_file.read_text(encoding="utf-8") == (
        "[global]\n"
        "repo1-s3-endpoint=https://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.r2.cloudflarestorage.com\n"
        "repo1-s3-bucket=offsite-dr\n"
        "repo1-s3-key=access-key\n"
        "repo1-s3-key-secret=secret-key-sentinel\n"
        "repo1-cipher-pass=cipher-pass-sentinel\n"
    )
    assert secret_dir.stat().st_mode & 0o777 == 0o750
    assert secret_file.stat().st_mode & 0o777 == 0o640
    assert secret_file.stat().st_uid == os.getuid()
    calls = log.read_text(encoding="utf-8")
    assert f"chown {os.getuid()}:70 {secret_dir}" in calls
    assert "chown 70:70" not in calls
    assert "chmod 0750" in calls and "chmod 0640" in calls
    validator_calls = validator_log.read_text(encoding="utf-8")
    assert validator_calls.count("secret_validation.py --secret-dir") == 2
    assert f"--secret-dir {secret_dir}" in validator_calls
    assert "secret-key-sentinel" not in completed.stdout + completed.stderr
    assert "cipher-pass-sentinel" not in completed.stdout + completed.stderr


def test_wizard_rerun_enter_keeps_all_persisted_values_and_metadata(tmp_path: Path) -> None:
    env, _, secret_file, _, _ = _wizard_fixture(tmp_path, existing=WIZARD_SECRET)
    before = _snapshot(secret_file)

    completed = _run_wizard(env, ["", "", "", ""])

    assert completed.returncode == 0, completed.stderr
    assert _snapshot(secret_file) == before


def test_wizard_partial_rerun_updates_only_selected_persisted_values(tmp_path: Path) -> None:
    env, _, secret_file, _, _ = _wizard_fixture(tmp_path, existing=WIZARD_SECRET)

    completed = _run_wizard(env, ["new-bucket", "", "new-secret", ""])

    assert completed.returncode == 0, completed.stderr
    text = secret_file.read_text(encoding="utf-8")
    assert "repo1-s3-endpoint=https://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.r2.cloudflarestorage.com" in text
    assert "repo1-s3-bucket=new-bucket" in text
    assert "repo1-s3-key=access-key-old" in text
    assert "repo1-s3-key-secret=new-secret" in text
    assert "repo1-cipher-pass=cipher-pass-old" in text
    assert "secret-key-old" not in completed.stdout + completed.stderr
    assert "cipher-pass-old" not in completed.stdout + completed.stderr


def test_wizard_rejects_existing_target_directory_before_replacement(tmp_path: Path) -> None:
    env, _, secret_file, log, _ = _wizard_fixture(
        tmp_path, existing=WIZARD_SECRET, target_kind="directory",
    )
    values = ["a" * 32, "offsite-dr", "access-key", "secret-key", "cipher-pass"]

    completed = _run_wizard(env, values)

    assert completed.returncode != 0
    assert secret_file.is_dir()
    assert not (secret_file / "r2.conf").exists()
    assert not log.exists() or "daemon-reload" not in log.read_text(encoding="utf-8")
    assert "secret-key" not in completed.stdout + completed.stderr
    assert "cipher-pass" not in completed.stdout + completed.stderr


def test_wizard_rejects_extra_direct_target_entry_before_replacement(tmp_path: Path) -> None:
    env, secret_dir, secret_file, log, _ = _wizard_fixture(
        tmp_path, existing=WIZARD_SECRET, extra_direct_file=True,
    )
    before = _snapshot(secret_file)

    completed = _run_wizard(env, ["", "", "", ""])

    assert completed.returncode != 0
    assert _snapshot(secret_file) == before
    assert (secret_dir / "extra.conf").read_text(encoding="utf-8") == "unexpected\n"
    assert not log.exists() or "daemon-reload" not in log.read_text(encoding="utf-8")


def test_wizard_identical_content_rerun_repairs_target_metadata(tmp_path: Path) -> None:
    env, secret_dir, secret_file, log, _ = _wizard_fixture(tmp_path, existing=WIZARD_SECRET)
    secret_dir.chmod(0o700)
    secret_file.chmod(0o600)

    completed = _run_wizard(env, ["", "", "", ""])

    assert completed.returncode == 0, completed.stderr
    assert secret_dir.stat().st_mode & 0o777 == 0o750
    assert secret_file.stat().st_mode & 0o777 == 0o640
    calls = log.read_text(encoding="utf-8")
    assert f"chown {os.getuid()}:70 {secret_dir} {secret_file}" in calls
    assert f"chmod 0640 {secret_file}" in calls


@pytest.mark.parametrize("failure", ("invalid", "eof", "validation", "interrupt"))
def test_wizard_failed_or_interrupted_rerun_preserves_file_and_metadata(
    tmp_path: Path, failure: str,
) -> None:
    mode = {"validation": "reject", "interrupt": "interrupt"}.get(failure, "ok")
    env, _, secret_file, _, _ = _wizard_fixture(tmp_path, existing=WIZARD_SECRET, validator_mode=mode)
    before = _snapshot(secret_file)
    values = {
        "invalid": ["INVALID-BUCKET"],
        "eof": [],
        "validation": ["", "", "", ""],
        "interrupt": ["", "", "", ""],
    }[failure]

    completed = _run_wizard(env, values)

    assert completed.returncode != 0
    assert _snapshot(secret_file) == before
    assert "secret-key-old" not in completed.stdout + completed.stderr
    assert "cipher-pass-old" not in completed.stdout + completed.stderr
