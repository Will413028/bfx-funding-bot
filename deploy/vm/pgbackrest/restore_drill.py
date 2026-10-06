#!/usr/bin/env python3
"""Run a bounded, isolated pgBackRest restore drill without shell execution."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[2]
COMPOSE_PATH = ROOT / "docker-compose.dr.yml"
CONFIG_PATH = SCRIPT_DIR / "pgbackrest.conf"
DEFAULT_SECRET_DIR = Path.home() / "bfx/pgbackrest/conf.d"
DEFAULT_OUTPUT_PATH = Path.home() / "bfx/dr-evidence/restore.json"
# The operator's acceptance drill writes restore.json; the recurring restore test its own receipt.
DEFAULT_LEDGER_OUTPUT_PATH = Path.home() / "bfx/dr-evidence/restore-ledger.json"
_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_RTO_SECONDS = 3600
_READ_FAILURE_CODES = frozenset({"production_read_failed", "restore_command_failed"})


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("restore_output_invalid")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_commands = _load_module("_bfx_restore_commands", SCRIPT_DIR / "restore_commands.py")
_evidence = _load_module("_bfx_restore_evidence", SCRIPT_DIR / "evidence.py")
_timing = _load_module("_bfx_restore_timing", SCRIPT_DIR / "restore_timing.py")
_secret_validation = _load_module(
    "_bfx_secret_validation", SCRIPT_DIR / "secret_validation.py"
)
RestoreInputError = _commands.RestoreInputError
RestoreResources = _commands.RestoreResources
build_restore_resources = _commands.build_restore_resources
EvidenceError = _evidence.EvidenceError
render_failure_evidence = _evidence.render_failure_evidence
_ledger = _evidence._ledger
SecretConfigError = _secret_validation.SecretConfigError
validate_secret_dir = _secret_validation.validate_secret_dir
StageTiming = _timing.StageTiming

_REQUIRED_TIMING_STAGES = frozenset(
    {
        "resource_setup",
        "physical_and_wal_recovery",
        "isolation_bootstrap",
        "verification",
        "cleanup",
    }
)


class DrillFailureError(ValueError):
    """A bounded restore failure code suitable for evidence, and optionally a bounded cause."""

    def __init__(self, code: str, cause: str | None = None) -> None:
        super().__init__(code)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class LedgerRequest:
    """Restore a backup, compare every scope's append-only ledger rows with production's
    within the restored copy's boundary, and run the image's read-only boot check. No
    operator baseline and no scope argument.

    Without ``backup_label`` this is the recurring restore test: the newest backup to the end
    of the archive. With it (and optionally ``target_time``), the operator's acceptance drill
    (Halt 2, incident acceptance) at that exact backup and recovery target; production's
    append-only ledger bounded by the restored copy is its baseline."""

    database_name: str = "bfx"
    production_container: str = "bfx-postgres"
    backup_label: str | None = None
    target_time: str | None = None


@dataclass(slots=True)
class _CreatedResources:
    network: bool = False
    egress_network: bool = False
    volume: bool = False
    container: bool = False


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
# (command, *, input_text, timeout, consume) -> (exit status, stderr tail); stdout is fed
# line by line.
StreamRunner = Callable[..., tuple[int, str]]
_STDERR_TAIL_BYTES = 2048


def _run_command(
    command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout, input=input_text, env=env
    )


def _stream_command(
    command: tuple[str, ...], *, input_text: str, timeout: float, consume: Callable[[bytes], None],
) -> tuple[int, str]:
    """Run `command`, feed `input_text` on stdin and every stdout line to `consume`.

    Output is never held in memory as a whole (production's ledger grows without bound); the
    process is killed when `timeout` expires, which surfaces as a negative status. Returns the
    status and the last `_STDERR_TAIL_BYTES` of stderr (drained on its own thread, so a chatty
    stderr cannot stall stdout), for classification only.
    """
    tail = bytearray()

    def drain(stream: object) -> None:
        for chunk in iter(lambda: stream.read(4096), b""):  # type: ignore[attr-defined]
            tail.extend(chunk)
            del tail[:-_STDERR_TAIL_BYTES]

    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        reader = threading.Thread(target=drain, args=(process.stderr,), daemon=True)
        reader.start()
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(input_text.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass
            for line in process.stdout:
                consume(line)
            status = process.wait()
            reader.join(timeout=5)
            return status, bytes(tail).decode("utf-8", errors="replace")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(8)}"


def _new_password() -> str:
    return secrets.token_hex(24)


def _failure(code: str) -> None:
    raise DrillFailureError(code)


def _compose_with_env(command: tuple[str, ...], env_path: Path) -> tuple[str, ...]:
    if command[:2] != ("docker", "compose"):
        _failure("restore_output_invalid")
    return (*command[:2], "--env-file", str(env_path), *command[2:])


def _generated_resource(name: str) -> bool:
    return re.fullmatch(r"bfx-dr-[a-z0-9-]+", name) is not None


def _validate_plan_resources(plan: RestoreResources) -> None:
    if not all(
        _generated_resource(name)
        for name in (
            plan.project_name,
            plan.volume_name,
            plan.network_name,
            plan.egress_network_name,
            plan.container_name,
            plan.verifier_container_name,
        )
    ):
        _failure("restore_output_invalid")
    if plan.sql_admin_role != "bfx":
        _failure("restore_output_invalid")


def _compose_environment() -> dict[str, str]:
    """Generated interpolation is authoritative; retain Docker transport and PATH."""
    return {
        key: value for key, value in os.environ.items()
        if key != "DATABASE_URL"
        and not key.startswith(("DR_", "POSTGRES_", "BFX_", "COMPOSE_"))
    }


def _config_is_clean_tracked(path: Path) -> bool:
    try:
        relative_path = path.relative_to(ROOT)
    except ValueError:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    git = ("git", "--literal-pathspecs", "-C", str(ROOT))
    commands = (
        (*git, "ls-files", "--error-unmatch", "--", str(relative_path)),
        (*git, "diff", "--quiet", "--", str(relative_path)),
        (*git, "diff", "--cached", "--quiet", "--", str(relative_path)),
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        if completed.returncode != 0:
            return False
    return True


def _write_json(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(report, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_timing_json(path: Path, report: dict[str, object]) -> None:
    """Persist non-authoritative timing separately from acceptance evidence."""
    _write_json(path, report)


def _invalidate_timing_json(path: Path) -> None:
    """Remove stale timing without entering the accepted-receipt code path."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        with path.open("r+") as handle:
            handle.truncate(0)


def _write_failure_log(output_path: Path, code: str) -> None:
    """Keep a bounded diagnostic marker without retaining command output."""
    log_path = output_path.with_name(f"{output_path.stem}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"restore drill failed: {code}\n"[:8192], encoding="utf-8")
    os.chmod(log_path, 0o600)


def _invalidate_evidence(path: Path) -> None:
    """Revoke stale green evidence even when atomic replacement runs out of space."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Truncation needs no new directory entry or data blocks, and also works
        # when the file is writable but its parent directory disallows unlink.
        with path.open("r+") as handle:
            handle.truncate(0)


_FALLBACK_INVALID_EVIDENCE = (
    b'{"error_code":"restore_output_invalid","kind":"restore",'
    b'"measured":false,"observed_at_ms":0,"schema_version":1}\n'
)


def _force_revoke_evidence(path: Path) -> bool:
    """Use an independent filesystem path to make failed evidence unaccepted."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass

    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(_FALLBACK_INVALID_EVIDENCE):
            written = os.write(descriptor, _FALLBACK_INVALID_EVIDENCE[offset:])
            if written <= 0:
                return False
            offset += written
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _revoke_after_failure(path: Path) -> bool:
    """Keep a failed high-level revoke from leaving an accepted old report."""
    try:
        _invalidate_evidence(path)
        return True
    except (KeyboardInterrupt, OSError):
        return _force_revoke_evidence(path)


def _unlink_env_file(path: Path, *, timeout: float) -> None:
    """Isolate potentially blocking filesystem IO in a killable child, not a thread."""
    subprocess.run(
        (
            sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)",
            str(path),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=timeout,
    )


class RestoreDrill:
    """Execute one isolated restore lifecycle using injectable command execution."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = _run_command,
        stream_runner: StreamRunner = _stream_command,
        config_path: Path = CONFIG_PATH,
        secret_dir: Path = DEFAULT_SECRET_DIR,
        output_path: Path = DEFAULT_OUTPUT_PATH,
        run_id_factory: Callable[[], str] = _new_run_id,
        password_factory: Callable[[], str] = _new_password,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        postgres_uid: int = 70,
        postgres_gid: int = 70,
    ) -> None:
        self._command_runner = command_runner
        self._stream_runner = stream_runner
        self._config_path = config_path
        self._secret_dir = secret_dir
        self._output_path = output_path
        self._run_id_factory = run_id_factory
        self._password_factory = password_factory
        self._clock = clock
        self._sleep = sleep
        self._postgres_uid = postgres_uid
        self._postgres_gid = postgres_gid
        self._deadline: float | None = None

    def _remaining(self) -> float:
        if self._deadline is None:
            _failure("restore_command_failed")
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            _failure("restore_command_failed")
        return remaining

    def _call(
        self, command: tuple[str, ...], *, timeout: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            kwargs: dict[str, object] = {}
            if command[:2] == ("docker", "compose"):
                kwargs["env"] = _compose_environment()
            if input_text is not None:
                kwargs["input_text"] = input_text
            remaining = self._remaining()
            kwargs["timeout"] = remaining if timeout is None else min(timeout, remaining)
            if kwargs["timeout"] <= 0:
                _failure("restore_command_failed")
            return self._command_runner(command, **kwargs)
        except Exception:
            _failure("restore_command_failed")

    def _require_success(
        self, command: tuple[str, ...], *, timeout: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = self._call(command, timeout=timeout, input_text=input_text)
        if completed.returncode != 0:
            _failure("restore_command_failed")
        return completed

    def _write_env_file(self, plan: RestoreResources, password: str) -> Path:
        database_url = (
            f"postgresql+asyncpg://{plan.verify_role}:{password}"
            f"@{plan.container_name}:5432/{plan.database_name}"
        )
        values = {
            "DR_PROJECT_NAME": plan.project_name,
            "DR_VOLUME_NAME": plan.volume_name,
            "DR_NETWORK_NAME": plan.network_name,
            "DR_EGRESS_NETWORK_NAME": plan.egress_network_name,
            "DR_CONTAINER_NAME": plan.container_name,
            "DR_SQL_ADMIN_ROLE": plan.sql_admin_role,
            "DR_DATABASE_NAME": plan.database_name,
            "DATABASE_URL": database_url,
            "BFX_DEPLOYMENT_ENV": "",
            "DR_TARGET_BACKUP_LABEL": plan.backup_label,
            "DR_TARGET_TIME": plan.target_time or "",
            "DR_PGBACKREST_SECRET_DIR": str(self._secret_dir),
        }
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="bfx-dr-", suffix=".env", delete=False,
            ) as handle:
                path = Path(handle.name)
                os.chmod(path, 0o600)
                for key, value in values.items():
                    handle.write(f"{key}={value}\n")
            return path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def _require_internal_network(self, plan: RestoreResources) -> None:
        completed = self._require_success(
            ("docker", "network", "inspect", "--format={{.Internal}}", plan.network_name)
        )
        if completed.stdout.strip() != "true":
            _failure("network_not_internal")

    def _require_external_egress(self, command: tuple[str, ...]) -> None:
        completed = self._require_success(command)
        if completed.stdout.strip() != "false":
            _failure("network_not_internal")

    def _require_egress_absent(
        self, command: tuple[str, ...], plan: RestoreResources
    ) -> None:
        completed = self._require_success(command)
        try:
            networks = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            _failure("restore_output_invalid")
        if not isinstance(networks, dict) or set(networks) != {plan.network_name}:
            _failure("restore_output_invalid")

    def _create_resources(self, plan: RestoreResources, created: _CreatedResources) -> None:
        self._require_success(plan.create_commands[0])
        created.network = True
        self._require_internal_network(plan)
        self._require_success(plan.create_commands[1])
        created.egress_network = True
        self._require_success(plan.create_commands[2])
        created.volume = True

    def _recover_restore(
        self, plan: RestoreResources, env_path: Path, created: _CreatedResources,
    ) -> None:
        created.container = True
        self._require_success(_compose_with_env(plan.run_commands[0], env_path))
        self._wait_for_health(plan)
        self._wait_for_recovery(plan)

    def _isolate_restore(self, plan: RestoreResources) -> None:
        self._require_external_egress(plan.run_commands[1])
        self._require_success(plan.run_commands[2])
        self._require_egress_absent(plan.run_commands[3], plan)

    def _wait_for_recovery(self, plan: RestoreResources) -> None:
        while True:
            completed = self._require_success(
                (
                    "docker", "exec", "--user", "postgres", "--interactive", plan.container_name,
                    "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                    "-h", "/var/run/postgresql", "-U", plan.sql_admin_role, "-d", plan.database_name,
                ),
                input_text="SELECT pg_is_in_recovery();\n",
            )
            self._remaining()
            result = completed.stdout.strip()
            if result == "f":
                return
            if result != "t":
                _failure("restore_output_invalid")
            self._sleep(min(1, self._remaining()))

    def _wait_for_health(self, plan: RestoreResources) -> None:
        deadline = self._clock() + min(600, self._remaining())
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                _failure("restore_command_failed")
            completed = self._require_success(
                ("docker", "inspect", "--format={{.State.Health.Status}}", plan.container_name),
                timeout=remaining,
            )
            if completed.stdout.strip() == "healthy":
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                _failure("restore_command_failed")
            # A container that has exited never becomes healthy; fail now instead of
            # spending the rest of the health deadline polling it.
            state = self._require_success(
                ("docker", "inspect", "--format={{.State.Status}}", plan.container_name),
                timeout=remaining,
            )
            if state.stdout.strip() in {"exited", "dead"}:
                _failure("restore_command_failed")
            remaining = deadline - self._clock()
            if remaining <= 0:
                _failure("restore_command_failed")
            self._sleep(min(1, remaining))

    def _elapsed_seconds(self, started: float) -> int:
        elapsed_seconds = math.ceil(self._clock() - started)
        if elapsed_seconds < 0 or elapsed_seconds > _MAX_RTO_SECONDS:
            _failure("rto_invalid")
        return elapsed_seconds

    def _bootstrap_role(
        self, plan: RestoreResources, password: str,
    ) -> None:
        # Connecting to the baseline database validates it exists before any SQL.
        # Send separate statements via psql stdin, with logging disabled before
        # the password-bearing statement is parsed/executed (including on error).
        role = f'"{plan.verify_role}"'
        # Exactly the tables the boot check reads (ledger_digest.VERIFIER_TABLES), those the
        # restored schema has (a newer release may know more); read-only by default too.
        names = ", ".join(f"'{name}'" for name in _ledger.VERIFIER_TABLES)
        access = (
            "DO $grant$ DECLARE name text; BEGIN "
            f"FOREACH name IN ARRAY ARRAY[{names}] LOOP "
            "IF to_regclass(format('public.%I', name)) IS NOT NULL THEN "
            f"EXECUTE format('GRANT SELECT ON TABLE public.%I TO %I', name, '{plan.verify_role}'); "
            "END IF; END LOOP; END $grant$;\n"
            f"ALTER ROLE {role} SET default_transaction_read_only = on;\n"
        )
        sql = (
            "SET log_statement = 'none';\n"
            "SET log_min_error_statement = 'panic';\n"
            "SET log_min_duration_statement = -1;\n"
            "SET log_min_duration_sample = -1;\n"
            "SET log_statement_sample_rate = 0;\n"
            "SET log_transaction_sample_rate = 0;\n"
            "SET log_duration = off;\n"
            "BEGIN;\n"
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '{password}';\n"
            f'GRANT CONNECT, TEMPORARY ON DATABASE "{plan.database_name}" TO {role};\n'
            f"GRANT USAGE ON SCHEMA public TO {role};\n"
            + access
            + "COMMIT;\n"
        )
        self._require_success(
            (
                "docker", "exec", "--user", "postgres", "--interactive", plan.container_name,
                "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", plan.sql_admin_role, "-d", plan.database_name,
            ),
            input_text=sql,
        )

    def _image_metadata(self, plan: RestoreResources) -> tuple[str, dict[str, str]]:
        completed = self._require_success(
            ("docker", "inspect", "--format={{.Image}}", plan.container_name)
        )
        image_id = completed.stdout.strip()
        if _SHA256.fullmatch(image_id) is None:
            _failure("restore_output_invalid")
        completed = self._require_success(
            ("docker", "image", "inspect", "--format={{json .Config.Labels}}", image_id)
        )
        return image_id, _evidence._parse_image_labels(completed.stdout)

    def _cleanup(
        self,
        plan: RestoreResources,
        env_path: Path | None,
        *,
        container_started: bool,
        verifier_started: bool,
        volume_created: bool,
        egress_network_created: bool,
        network_created: bool,
    ) -> bool:
        # Cleanup is independent of the restore budget, including after timeout.
        self._deadline = self._clock() + 30
        failed = False
        # A timed-out Compose client can leave its one-off container running.
        # The generated name is eligible before starting the client.
        if verifier_started:
            try:
                result = self._call(plan.cleanup_commands[4], timeout=min(10, self._remaining() - 1))
                if result.returncode != 0:
                    # --rm may already have removed it. Confirm absence without
                    # interpreting or exposing Docker's raw error text.
                    remaining = self._require_success((
                        "docker", "container", "ls", "--all", "--filter",
                        f"name=^/{plan.verifier_container_name}$", "--format={{.Names}}",
                    ))
                    if remaining.stdout.strip():
                        failed = True
                self._remaining()
            except DrillFailureError:
                failed = True
        # Compose needs the env file. Reserve a second for unlink, then spend
        # the rest of this same cleanup budget on the known-created resources.
        if container_started and env_path is not None:
            try:
                command = _compose_with_env(plan.cleanup_commands[0], env_path)
                if self._call(command, timeout=self._remaining() - 1).returncode != 0:
                    failed = True
            except DrillFailureError:
                failed = True
        if env_path is not None:
            try:
                _unlink_env_file(env_path, timeout=self._remaining())
                self._remaining()
            except (OSError, subprocess.SubprocessError, DrillFailureError):
                failed = True
        commands: list[tuple[str, ...]] = []
        if volume_created:
            commands.append(plan.cleanup_commands[1])
        if egress_network_created:
            commands.append(plan.cleanup_commands[2])
        if network_created:
            commands.append(plan.cleanup_commands[3])
        for command in commands:
            try:
                if self._call(command).returncode != 0:
                    failed = True
                self._remaining()
            except DrillFailureError:
                failed = True
        return failed

    def _latest_backup_label(self, request: LedgerRequest) -> str:
        """Newest backup set, read from the production stanza (read-only `info`)."""
        if _CONTAINER_NAME.fullmatch(request.production_container) is None:
            _failure("restore_output_invalid")
        try:
            completed = self._command_runner(
                ("docker", "exec", "--user", "postgres", request.production_container,
                 "pgbackrest", "--stanza=bfx", "info", "--output=json"),
                timeout=120,
            )
        except Exception:
            _failure("backup_label_unavailable")
        if completed.returncode != 0:
            _failure("backup_label_unavailable")
        try:
            return str(_evidence._parse_pgbackrest_info(completed.stdout))
        except EvidenceError:
            _failure("backup_label_unavailable")

    def _ledger_plan(self, request: LedgerRequest, backup_label: str) -> RestoreResources:
        try:
            plan = build_restore_resources(
                backup_label=backup_label, target_time=request.target_time,
                run_id=self._run_id_factory(), database_name=request.database_name,
            )
        except RestoreInputError:
            _failure("restore_output_invalid")
        _validate_plan_resources(plan)
        if not _config_is_clean_tracked(self._config_path) or not COMPOSE_PATH.is_file():
            _failure("restore_output_invalid")
        try:
            validate_secret_dir(
                self._secret_dir,
                postgres_uid=self._postgres_uid,
                postgres_gid=self._postgres_gid,
            )
        except SecretConfigError:
            _failure("restore_output_invalid")
        return plan

    @staticmethod
    def _psql(container: str, plan: RestoreResources) -> tuple[str, ...]:
        """The admin role on the container's own socket (no password, no network path)."""
        return ("docker", "exec", "--user", "postgres", "--interactive", container,
                "psql", "-X", "-qAt", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", plan.sql_admin_role, "-d", plan.database_name)

    def _ledger_boot(self, plan: RestoreResources, image: str, env_path: Path) -> str:
        completed = self._call(
            _commands.ledger_verifier_command(plan, image=image, env_path=env_path),
        )
        if completed.returncode == 3:
            _failure(_ledger.boot_failure_code(completed.stdout))
        if completed.returncode != 0:
            _failure("restore_command_failed")
        return completed.stdout

    def _ledger_stream(
        self, container: str, plan: RestoreResources, bounds: object, *, restored: bool,
        digest: object, failure_code: str,
    ) -> float:
        """Stream one cluster's bounded COPY into `digest`; returns the read's seconds."""
        started = self._clock()
        remaining = self._remaining()
        # The server ends the snapshot a little before the client is killed.
        script = _ledger.digest_script(bounds, restored=restored,
                                       timeout_ms=max(1, int(remaining * 1000) - 1000))
        try:
            status, stderr = self._stream_runner(
                self._psql(container, plan), input_text=script,
                timeout=remaining, consume=digest,
            )
        except Exception:
            _failure(failure_code)
        if status != 0:
            # The psql argv carries no DSN or password (admin role on the container's socket),
            # but an error's DETAIL can quote row values: only the bounded cause leaves here.
            raise DrillFailureError(failure_code, _ledger.classify_read_failure(status, stderr))
        self._remaining()
        return round(max(0.0, self._clock() - started), 3)

    def run(self, request: LedgerRequest) -> int:
        plan: RestoreResources | None = None
        env_path: Path | None = None
        timing_output_path = self._output_path.with_name(
            f"{self._output_path.stem}-timing.json"
        )
        timing = StageTiming(clock=self._clock)
        timing_stage: str | None = None
        timing_usable = True
        created = _CreatedResources()
        verifier_cleanup_eligible = False
        rto_started: float | None = None
        failure_code: str | None = None
        failure_cause: str | None = None
        success_report: dict[str, object] | None = None
        cleanup_failed = False
        failure_persist_failed = False

        def begin_timing(name: str) -> None:
            nonlocal timing_stage, timing_usable
            if not timing_usable:
                return
            try:
                timing.begin(name)
                timing_stage = name
            except Exception:
                timing_stage = None
                timing_usable = False

        def end_timing(name: str) -> None:
            nonlocal timing_stage, timing_usable
            if not timing_usable:
                return
            try:
                timing.end(name)
                timing_stage = None
            except Exception:
                timing_stage = None
                timing_usable = False

        try:
            try:
                _invalidate_evidence(self._output_path)
            except (KeyboardInterrupt, OSError):
                failure_code = "evidence_invalidation_failed"
                if not _revoke_after_failure(self._output_path):
                    failure_persist_failed = True
            try:
                _invalidate_timing_json(timing_output_path)
            except OSError:
                # Timing is diagnostic only. The accepted restore receipt keeps
                # its existing invalidation and failure behavior above.
                timing_usable = False
            if failure_code is None:
                label = request.backup_label or self._latest_backup_label(request)
                plan = self._ledger_plan(request, label)
                password = self._password_factory()
                if not isinstance(password, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password) is None:
                    _failure("restore_output_invalid")
                env_path = self._write_env_file(plan, password)
                rto_started = self._clock()
                self._deadline = rto_started + _MAX_RTO_SECONDS
                begin_timing("resource_setup")
                verifier_image = self._require_success((
                    "docker", "image", "inspect", "--format={{.Id}}", "bfx-bot:local",
                )).stdout.strip()
                if not _evidence.is_image_digest(verifier_image):
                    _failure("restore_output_invalid")
                self._create_resources(plan, created)
                end_timing("resource_setup")
                begin_timing("physical_and_wal_recovery")
                self._recover_restore(plan, env_path, created)
                end_timing("physical_and_wal_recovery")
                begin_timing("isolation_bootstrap")
                self._isolate_restore(plan)
                self._bootstrap_role(plan, password)
                end_timing("isolation_bootstrap")
                begin_timing("verification")
                # W comes from the restored copy; production is only read, after isolation.
                bounds = _ledger.parse_bounds(self._require_success(
                    self._psql(plan.container_name, plan), input_text=_ledger.bounds_script(),
                ).stdout)
                verifier_cleanup_eligible = True
                boot = _ledger.parse_boot(self._ledger_boot(plan, verifier_image, env_path), bounds)
                tables = _ledger.compared_tables(bounds)
                restored = _ledger.StreamDigest(tables)
                restored_seconds = self._ledger_stream(
                    plan.container_name, plan, bounds, restored=True, digest=restored,
                    failure_code="restore_command_failed")
                production = _ledger.StreamDigest(tables)
                production_seconds = self._ledger_stream(
                    request.production_container, plan, bounds, restored=False,
                    digest=production, failure_code="production_read_failed")
                ledger = _ledger.compare(bounds, restored, production)
                # Visible growth: the ADR's revocation trigger is a read nearing the budget.
                ledger["read_seconds"] = {"restored": restored_seconds,
                                          "production": production_seconds}
                image_digest, image_labels = self._image_metadata(plan)
                elapsed_seconds = self._elapsed_seconds(rto_started)
                self._remaining()
                success_report = _evidence.render_ledger_restore_evidence(
                    kind=_evidence.LEDGER_KIND, bounds=bounds, ledger=ledger, boot=boot,
                    target_backup_label=label, target_time=request.target_time,
                    restore_test=request.backup_label is None, elapsed_seconds=elapsed_seconds,
                    observed_at_ms=time.time_ns() // 1_000_000, config_path=self._config_path,
                    image_digest=image_digest, image_labels=image_labels,
                    network_name=plan.network_name, network_internal=True,
                    egress_disconnected=True, verifier_image_digest=verifier_image,
                )
                success_report["rto_seconds"] = self._elapsed_seconds(rto_started)
                success_report["restore_run_id"] = plan.project_name.removeprefix("bfx-dr-")
                self._remaining()
                end_timing("verification")
        except DrillFailureError as exc:
            failure_code = str(exc)
            failure_cause = exc.cause
            if failure_cause is not None:
                print(f"restore drill: {failure_code}: cause={failure_cause}", file=sys.stderr)
        except EvidenceError as exc:
            failure_code = str(exc)
        except _ledger.LedgerVerificationError as exc:
            failure_code = str(exc)
            if exc.detail:
                # Table names only (never row data), for the journal.
                print(f"restore drill: {failure_code}: {', '.join(exc.detail)}", file=sys.stderr)
        except (OSError, ValueError, TypeError):
            failure_code = "restore_output_invalid"
        finally:
            if timing_stage is not None:
                end_timing(timing_stage)
            if plan is not None:
                begin_timing("cleanup")
                cleanup_failed = self._cleanup(
                    plan, env_path, container_started=created.container,
                    verifier_started=verifier_cleanup_eligible,
                    volume_created=created.volume,
                    egress_network_created=created.egress_network,
                    network_created=created.network,
                )
                end_timing("cleanup")
                if cleanup_failed:
                    failure_code = "cleanup_failed"
            if success_report is not None and failure_code is None:
                try:
                    self._remaining()
                    _write_json(self._output_path, success_report)
                    self._remaining()
                except KeyboardInterrupt:
                    if not _revoke_after_failure(self._output_path):
                        failure_persist_failed = True
                    raise
                except (DrillFailureError, OSError, TypeError, ValueError):
                    failure_code = "evidence_persist_failed"
                    if not _revoke_after_failure(self._output_path):
                        failure_persist_failed = True
            if failure_code is not None:
                failure_kind = _evidence.LEDGER_KIND
                try:
                    report = render_failure_evidence(
                        kind=failure_kind, error_code=failure_code,
                        observed_at_ms=time.time_ns() // 1_000_000,
                        # A later cleanup or persist failure replaces the code, not its cause.
                        cause=failure_cause if failure_code in _READ_FAILURE_CODES else None,
                    )
                except EvidenceError:
                    report = render_failure_evidence(
                        kind=failure_kind, error_code="restore_output_invalid",
                        observed_at_ms=time.time_ns() // 1_000_000,
                    )
                try:
                    _write_json(self._output_path, report)
                except OSError:
                    failure_persist_failed = True
                    if not _revoke_after_failure(self._output_path):
                        failure_persist_failed = True
                # A full disk or failed invalidation must not skip the separate,
                # bounded diagnostic marker; no raw exception text is persisted.
                try:
                    _write_failure_log(
                        self._output_path,
                        "cleanup_failed" if cleanup_failed else str(report["error_code"]),
                    )
                except OSError:
                    failure_persist_failed = True
            if timing_usable:
                try:
                    stages = timing.render()
                    complete = (
                        failure_code is None
                        and not failure_persist_failed
                        and success_report is not None
                        and set(stages) == _REQUIRED_TIMING_STAGES
                    )
                    _write_timing_json(
                        timing_output_path,
                        {
                            "schema_version": 1,
                            "kind": "restore_timing",
                            "restore_run_id": (
                                plan.project_name.removeprefix("bfx-dr-")
                                if plan is not None
                                else None
                            ),
                            "complete": complete,
                            "stages": stages,
                        },
                    )
                except Exception:
                    # Diagnostics must never alter accepted receipt semantics.
                    with suppress(OSError):
                        _invalidate_timing_json(timing_output_path)
        if failure_code is not None or failure_persist_failed:
            return 2
        if success_report is None:
            return 2
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-label",
                        help="the operator's acceptance drill (Halt 2, incident acceptance): "
                             "restore this backup set instead of the newest")
    parser.add_argument("--target-time", help="with --backup-label: PITR target (UTC, ...Z)")
    parser.add_argument(
        "--restore-test", action="store_true",
        help="the recurring, baseline-free restore test; this release runs ledger mode: restore "
             "the newest backup to the end of the archive, compare the ledger with production's "
             "within the restored boundary and run the image's read-only boot check. The caller "
             "(the installed restore-test wrapper) names no mode, only --output, and accepts a "
             "fresh measured receipt with restore_test: true",
    )
    parser.add_argument("--output", type=Path,
                        help="receipt path (default restore-ledger.json for --restore-test, "
                             "restore.json for the acceptance drill)")
    parser.add_argument("--database-name", default="bfx")
    parser.add_argument("--production-container",
                        help="the production cluster's container (default bfx-postgres)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.output is not None and not args.output.is_absolute():
        parser.error("--output must be an absolute path")
    production_container = args.production_container or "bfx-postgres"
    if args.restore_test:
        if args.backup_label is not None or args.target_time is not None:
            parser.error("--restore-test takes only --output/--database-name/--production-container")
        return RestoreDrill(output_path=args.output or DEFAULT_LEDGER_OUTPUT_PATH).run(
            LedgerRequest(database_name=args.database_name,
                          production_container=production_container))
    # The acceptance drill: the same ledger verification at the operator's backup and target.
    if args.backup_label is None:
        parser.error("name a mode: --restore-test, or --backup-label [--target-time] for the "
                     "acceptance drill (every scope is verified)")
    return RestoreDrill(output_path=args.output or DEFAULT_OUTPUT_PATH).run(LedgerRequest(
        database_name=args.database_name, production_container=production_container,
        backup_label=args.backup_label, target_time=args.target_time,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
