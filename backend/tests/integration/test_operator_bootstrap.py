"""Real disposable PostgreSQL/Redis contract for the Node operator bootstrap."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_ROOT = REPO_ROOT / "frontend"
OPERATOR_ID = "operator-fixture"
OTHER_ID = "other-fixture"
NODE_TOOL = ["npx", "--yes", "--package=node@22", "--package=pnpm@10", "-c"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _redis_command(redis_url: str, *parts: str) -> str | int | None:
    parsed = urlparse(redis_url)
    payload = f"*{len(parts)}\r\n".encode()
    for part in parts:
        encoded = part.encode()
        payload += f"${len(encoded)}\r\n".encode() + encoded + b"\r\n"
    with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 6379)) as conn:
        conn.sendall(payload)
        response = conn.recv(2 * 1024 * 1024)
    prefix, body = response[:1], response[1:]
    if prefix == b"+":
        return body.split(b"\r\n", 1)[0].decode()
    if prefix == b":":
        return int(body.split(b"\r\n", 1)[0])
    if prefix == b"$":
        length_bytes, value = body.split(b"\r\n", 1)
        length = int(length_bytes)
        return None if length == -1 else value[:length].decode()
    raise AssertionError("isolated Redis returned an unexpected response")


def _start_redis(executable: str, data_dir: Path) -> tuple[subprocess.Popen[bytes], str]:
    """Start redis-server on a free port, retrying when another process takes it first.

    A probed port can be taken between the probe and redis's bind (another xdist worker, a
    Docker port mapping). Then our redis exits at once; a PONG only counts while our own
    process is still alive.
    """
    for _attempt in range(10):
        port = _free_port()
        process = subprocess.Popen(
            [executable, "--bind", "127.0.0.1", "--protected-mode", "yes", "--port", str(port),
             "--save", "", "--appendonly", "no", "--dir", str(data_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        redis_url = f"redis://127.0.0.1:{port}/0"
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            try:
                if _redis_command(redis_url, "PING") == "PONG" and process.poll() is None:
                    return process, redis_url
            except OSError:
                pass
            time.sleep(0.05)
        if process.poll() is None:  # alive but never answered: not a bind race
            process.kill()
            process.wait(timeout=5)
            break
        process.wait(timeout=5)  # exited at once: the port was taken, try another
    pytest.fail("disposable Redis did not become ready")


@pytest.fixture(scope="module")
def isolated_redis(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is unavailable for the disposable integration test")
    process, redis_url = _start_redis(executable, tmp_path_factory.mktemp("operator-bootstrap-redis"))
    try:
        yield redis_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def auth_database(pg_head_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh copy of the database Alembic migrated from empty to head."""
    monkeypatch.setenv("DATABASE_URL", pg_head_url)
    yield pg_head_url.replace("postgresql+psycopg://", "postgresql://")


def _seed_operator(database_url: str, *, other_role: str = "user") -> None:
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.executemany(
            """
                INSERT INTO auth."user"
                    (id, name, email, "emailVerified", "createdAt", "updatedAt",
                     role, banned, "twoFactorEnabled")
                VALUES (%s, %s, %s, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        %s, false, %s)
                """,
            [
                (OPERATOR_ID, "Operator", "operator@test.invalid", "user", True),
                (OTHER_ID, "Other", "other@test.invalid", other_role, False),
            ],
        )
        cursor.execute(
            """
                INSERT INTO auth."account"
                    (id, "accountId", "providerId", "userId", password,
                     "createdAt", "updatedAt")
                VALUES ('credential-fixture', %s, 'credential', %s,
                        'synthetic-password-hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
            (OPERATOR_ID, OPERATOR_ID),
        )
        cursor.execute(
            """
                INSERT INTO auth."twoFactor"
                    (id, secret, "backupCodes", "userId", verified)
                VALUES ('totp-fixture', 'synthetic-encrypted-secret',
                        'synthetic-encrypted-backup-codes', %s, true)
                """,
            (OPERATOR_ID,),
        )


def _node_env(database_url: str, redis_url: str) -> dict[str, str]:
    inherited = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ}
    return {
        **inherited,
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "BFX_OPERATOR_USER_ID": OPERATOR_ID,
        "BFX_OPERATOR_ROLE": "admin",
    }


def _run_cli(
    database_url: str,
    redis_url: str,
    arguments: list[str],
) -> subprocess.CompletedProcess[str]:
    command_text = "node scripts/bootstrap-operator.mjs " + " ".join(
        shlex.quote(argument) for argument in arguments
    )
    return subprocess.run(
        [*NODE_TOOL, command_text],
        cwd=FRONTEND_ROOT,
        env=_node_env(database_url, redis_url),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _record(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, (result.stdout, result.stderr)
    return json.loads(lines[-1])


def _auth_state(database_url: str) -> dict[str, object]:
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            'SELECT role, banned, "twoFactorEnabled", "emailVerified" '
            'FROM auth."user" WHERE id = %s',
            (OPERATOR_ID,),
        )
        role, banned, two_factor_enabled, email_verified = cursor.fetchone()
        cursor.execute(
            'SELECT verified FROM auth."twoFactor" WHERE "userId" = %s',
            (OPERATOR_ID,),
        )
        (totp_verified,) = cursor.fetchone()
        cursor.execute(
            'SELECT password FROM auth."account" WHERE "userId" = %s',
            (OPERATOR_ID,),
        )
        (password,) = cursor.fetchone()
    return {
        "role": role,
        "banned": banned,
        "twoFactorEnabled": two_factor_enabled,
        "emailVerified": email_verified,
        "totpVerified": totp_verified,
        "password": password,
    }


def _seed_sessions(redis_url: str) -> None:
    operator_inventory = json.dumps([{"token": "operator-session", "expiresAt": 4_102_444_800_000}])
    other_inventory = json.dumps([{"token": "other-session", "expiresAt": 4_102_444_800_000}])
    for key, value in (
        (f"active-sessions-{OPERATOR_ID}", operator_inventory),
        ("operator-session", "operator-session-body"),
        ("bfx:mfa-verified:operator-session", "1"),
        (f"active-sessions-{OTHER_ID}", other_inventory),
        ("other-session", "other-session-body"),
        ("bfx:mfa-verified:other-session", "1"),
        ("unrelated-key", "keep-me"),
    ):
        assert _redis_command(redis_url, "SET", key, value) == "OK"


def test_default_dry_run_changes_no_auth_or_session_state(
    auth_database: str,
    isolated_redis: str,
    tmp_path: Path,
) -> None:
    _seed_operator(auth_database)
    _seed_sessions(isolated_redis)
    before = _auth_state(auth_database)
    audit = tmp_path / "dry-run.json"

    result = _run_cli(
        auth_database,
        isolated_redis,
        ["--audit-file", str(audit)],
    )

    assert result.returncode == 0, result.stderr
    record = _record(result)
    assert record["status"] == "completed"
    assert record["mode"] == "dry-run"
    assert record["operatorUserId"] == OPERATOR_ID
    assert record["dbCommitted"] is False
    assert record["revokedKeyCount"] == 0
    assert _auth_state(auth_database) == before
    assert _redis_command(isolated_redis, "GET", "operator-session") == ("operator-session-body")
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600


def test_apply_assigns_only_role_and_revokes_only_operator_sessions(
    auth_database: str,
    isolated_redis: str,
    tmp_path: Path,
) -> None:
    _seed_operator(auth_database)
    _seed_sessions(isolated_redis)
    audit = tmp_path / "apply.json"

    result = _run_cli(
        auth_database,
        isolated_redis,
        [
            "--apply",
            "--confirm-bootstrap-admin",
            "--audit-file",
            str(audit),
        ],
    )

    assert result.returncode == 0, result.stderr
    record = _record(result)
    assert record["status"] == "completed"
    assert record["dbCommitted"] is True
    assert record["roleChanged"] is True
    assert record["revokedSessionCount"] == 1
    assert _auth_state(auth_database) == {
        "role": "admin",
        "banned": False,
        "twoFactorEnabled": True,
        "emailVerified": False,
        "totpVerified": True,
        "password": "synthetic-password-hash",
    }
    assert _redis_command(isolated_redis, "GET", f"active-sessions-{OPERATOR_ID}") is None
    assert _redis_command(isolated_redis, "GET", "operator-session") is None
    assert _redis_command(isolated_redis, "GET", "bfx:mfa-verified:operator-session") is None
    assert _redis_command(isolated_redis, "GET", "other-session") == "other-session-body"
    assert _redis_command(isolated_redis, "GET", "bfx:mfa-verified:other-session") == "1"
    assert _redis_command(isolated_redis, "GET", "unrelated-key") == "keep-me"
    assert json.loads(audit.read_text()) == record


def test_conflicting_admin_rolls_back_without_session_deletion(
    auth_database: str,
    isolated_redis: str,
    tmp_path: Path,
) -> None:
    _seed_operator(auth_database, other_role="user,admin")
    _seed_sessions(isolated_redis)
    audit = tmp_path / "conflict.json"

    result = _run_cli(
        auth_database,
        isolated_redis,
        [
            "--apply",
            "--confirm-bootstrap-admin",
            "--audit-file",
            str(audit),
        ],
    )

    assert result.returncode == 1
    assert result.stdout == ""
    receipt = json.loads(audit.read_text())
    assert receipt["status"] == "failed"
    assert receipt["failureCode"] == "admin_conflict"
    assert receipt["dbCommitted"] is False
    assert _auth_state(auth_database)["role"] == "user"
    assert _redis_command(isolated_redis, "GET", "operator-session") == ("operator-session-body")


def test_standalone_cli_module_import_has_no_execution_side_effect() -> None:
    result = subprocess.run(
        [
            *NODE_TOOL,
            "node --input-type=module -e \"await import('./scripts/bootstrap-operator.mjs'); console.log('imported')\"",
        ],
        cwd=FRONTEND_ROOT,
        env={key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"
