"""Execute the restore entrypoint at its filesystem/command safety boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/vm/postgres/dr-restore-entrypoint.sh"


@pytest.mark.parametrize("entry", ["PG_VERSION", ".hidden"])
def test_refuses_unapproved_data_path_without_touching_existing_files(
    tmp_path: Path, entry: str,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / entry
    sentinel.write_text("must survive")
    commands = tmp_path / "bin"
    commands.mkdir()
    fake = commands / "pgbackrest"
    fake.write_text("#!/bin/sh\nprintf called > \"$CALL_LOG\"\nexit 99\n")
    fake.chmod(0o755)
    call_log = tmp_path / "calls"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
            "PGDATA": str(data), "DR_VOLUME_NAME": "bfx-dr-test-data",
            "DR_TARGET_BACKUP_LABEL": "20260909-140653F_20260909-140833D",
            "DR_TARGET_TIME": "", "CALL_LOG": str(call_log),
        },
        capture_output=True, text=True, check=False,
    )
    assert sentinel.exists(), "restore must not erase an unapproved data directory"
    assert sentinel.read_text() == "must survive"
    assert not call_log.exists(), "unsafe targets must fail before contacting the repository"
    assert result.returncode == 2


def run_function(body: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; ' + body, "test", str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("entry", ["18/docker/PG_VERSION", "18/docker/.hidden", "18/other", "other"])
def test_rejects_nonempty_restore_volume_without_deleting(tmp_path: Path, entry: str) -> None:
    sentinel = tmp_path / entry
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep")
    result = run_function('validate_empty_target "$1" "$2"', str(tmp_path), str(tmp_path / "18/docker"))
    assert result.returncode == 2
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("relative", ["18", "18/docker"])
def test_rejects_symlink_in_restore_path(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "volume"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    result = run_function('validate_empty_target "$1" "$2"', str(root), str(root / "18/docker"))
    assert result.returncode == 2
    assert link.is_symlink()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("existing_subdirs", [False, True])
def test_allows_only_empty_versioned_target(tmp_path: Path, existing_subdirs: bool) -> None:
    data = tmp_path / "18/docker"
    if existing_subdirs:
        data.mkdir(parents=True)
    result = run_function('validate_empty_target "$1" "$2"', str(tmp_path), str(data))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("", ["--type=default"]),
        ("2026-09-09T14:10:00Z", ["--type=time", "--target=2026-09-09 14:10:00+00", "--target-action=promote"]),
    ],
)
def test_restore_argv_preserves_archive_end_or_explicit_time(target: str, expected: list[str]) -> None:
    result = run_function(
        'gosu() { printf "runner:%s\\n" "$1"; shift; "$@"; }; '
        'pgbackrest() { printf "%s\\n" "$@"; }; restore_backup "$1" "$2"',
        "20260909-140653F_20260909-140833D", target,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "runner:postgres", "--stanza=bfx", "--set=20260909-140653F_20260909-140833D", *expected, "restore",
    ]


def test_failed_restore_does_not_continue() -> None:
    result = run_function(
        'gosu() { shift; "$@"; }; pgbackrest() { return 47; }; '
        'restore_backup backup ""; echo unsafe_continue',
    )
    assert result.returncode == 47
    assert "unsafe_continue" not in result.stdout


@pytest.mark.parametrize(
    ("volume", "backup", "target"),
    [("bfx_pgdata", "backup", ""), ("bfx-dr-../data", "backup", ""),
     ("bfx-dr-test", "", ""), ("bfx-dr-test", "--bad", ""),
     ("bfx-dr-test", "backup", "bad time")],
)
def test_invalid_identity_or_recovery_input_fails_closed(volume: str, backup: str, target: str) -> None:
    result = run_function('validate_restore_inputs "$1" "$2" "$3"', volume, backup, target)
    assert result.returncode == 2


def test_directory_preparation_covers_parent_and_data(tmp_path: Path) -> None:
    data = tmp_path / "18/docker"
    result = run_function(
        'chown() { printf "%s\\n" "$@"; }; prepare_restore_directories "$1" "$2"',
        str(tmp_path), str(data),
    )
    assert result.returncode == 0, result.stderr
    assert data.is_dir()
    assert (tmp_path / "18").stat().st_mode & 0o777 == 0o700
    assert data.stat().st_mode & 0o777 == 0o700
    assert result.stdout.splitlines() == ["70:70", str(tmp_path / "18"), str(data)]


def test_unreadable_configuration_fails_with_safe_marker() -> None:
    result = run_function(
        'gosu() { return 1; }; check_postgres_access /data /config /secret; echo unsafe_continue',
    )
    assert result.returncode == 2
    assert result.stderr.strip() == "restore_permissions_invalid"
    assert "unsafe_continue" not in result.stdout


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "expected"),
    [("empty", 42), ("existing", 2), ("hidden", 2), ("symlink", 2),
     ("config_private", 2), ("secret_private", 2), ("secret_symlink", 2)],
)
def test_image_preflight_under_real_postgres_uid(case: str, expected: int) -> None:
    """Only the external R2 operation is replaced; mounts and UID checks are real.

    Run explicitly with BFX_DR_TEST_IMAGE set to the pinned local DR image.
    Never use a production volume or credential in these tests.
    """
    image = os.environ.get("BFX_DR_TEST_IMAGE")
    if not image:
        pytest.skip("set BFX_DR_TEST_IMAGE for isolated Docker preflight tests")
    volume = "bfx-dr-test-" + uuid.uuid4().hex
    subject = volume + "-subject"
    verifier = volume + "-verifier"
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    setup = r'''
mkdir -p /etc/pgbackrest/conf.d
printf '[global]\n' > /etc/pgbackrest/conf.d/r2.conf
chown 70:70 /etc/pgbackrest/conf.d /etc/pgbackrest/conf.d/r2.conf
chmod 750 /etc/pgbackrest/conf.d
chmod 640 /etc/pgbackrest/conf.d/r2.conf
chmod 644 /etc/pgbackrest/pgbackrest.conf
case "$TEST_CASE" in
  existing) mkdir -p "$PGDATA"; printf keep > "$PGDATA/PG_VERSION" ;;
  hidden) mkdir -p "$PGDATA"; printf keep > "$PGDATA/.hidden" ;;
  symlink) mkdir -p /tmp/other; ln -s /tmp/other /var/lib/postgresql/18 ;;
  config_private) chown 0:0 /etc/pgbackrest/pgbackrest.conf; chmod 600 /etc/pgbackrest/pgbackrest.conf ;;
  secret_private) chown 0:0 /etc/pgbackrest/conf.d/r2.conf; chmod 600 /etc/pgbackrest/conf.d/r2.conf ;;
  secret_symlink) ln -s /etc/pgbackrest/conf.d/r2.conf /etc/pgbackrest/conf.d/a.conf ;;
esac
printf '#!/bin/sh\ntest "$(id -u)" = 70 || exit 43\nexit 42\n' > /usr/local/bin/pgbackrest
chmod 755 /usr/local/bin/pgbackrest
exec bash /subject.sh
'''
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", subject, "--pull=never", "--network=none",
             "--mount", f"type=volume,src={volume},dst=/var/lib/postgresql",
             "--mount", f"type=bind,src={SCRIPT},dst=/subject.sh,readonly",
             "--env", f"DR_VOLUME_NAME={volume}", "--env", "DR_TARGET_BACKUP_LABEL=backup",
             "--env", "DR_TARGET_TIME=", "--env", f"TEST_CASE={case}",
             "--entrypoint", "/bin/bash", image, "-ec", setup],
            capture_output=True, text=True, check=False, timeout=60,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if case == "empty":
            subprocess.run(
                ["docker", "run", "--rm", "--name", verifier, "--pull=never", "--network=none", "--user=70:70",
                 "--mount", f"type=volume,src={volume},dst=/inspect,readonly",
                 "--entrypoint", "/bin/sh", image, "-ec",
                 "test -x /inspect/18 && test -r /inspect/18/docker"],
                check=True, capture_output=True, timeout=30,
            )
        elif case in {"existing", "hidden"}:
            leaf = "PG_VERSION" if case == "existing" else ".hidden"
            sentinel = subprocess.run(
                ["docker", "run", "--rm", "--name", verifier, "--pull=never", "--network=none",
                 "--mount", f"type=volume,src={volume},dst=/inspect,readonly",
                 "--entrypoint", "/bin/cat", image, f"/inspect/18/docker/{leaf}"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            assert sentinel.stdout == "keep"
    finally:
        original_error = sys.exception()
        try:
            # Timeout kills the CLI, not necessarily its container. Remove only
            # this test's named containers before attempting the volume.
            subprocess.run(
                ["docker", "rm", "--force", subject, verifier],
                check=False, capture_output=True, timeout=20,
            )
            subprocess.run(
                ["docker", "volume", "rm", volume],
                check=True, capture_output=True, timeout=20,
            )
        except (subprocess.SubprocessError, OSError) as cleanup_error:
            if original_error is not None:
                original_error.add_note(f"Docker fixture cleanup failed: {type(cleanup_error).__name__}")
            else:
                raise
