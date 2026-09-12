#!/usr/bin/env python3
"""Run a bounded, isolated pgBackRest restore drill without shell execution."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
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
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_REPLAY_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_RTO_SECONDS = 3600


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
RestorePlan = _commands.RestorePlan
build_restore_plan = _commands.build_restore_plan
EvidenceError = _evidence.EvidenceError
render_failure_evidence = _evidence.render_failure_evidence
render_restore_evidence = _evidence.render_restore_evidence
RestoreBaseline = _evidence.RestoreBaseline
load_restore_baseline = _evidence.load_restore_baseline
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
    """A bounded restore failure code suitable for evidence."""


@dataclass(frozen=True, slots=True)
class DrillRequest:
    account_id: str
    environment: str
    projector_version: str
    backup_label: str
    target_time: str | None
    baseline_path: Path
    archive_only: bool = False
    target_run_id: str | None = None


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run_command(
    command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout, input=input_text, env=env
    )


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


def _validate_plan_resources(plan: RestorePlan) -> None:
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
    log_path = output_path.with_name("restore.log")
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

    def _validate_prerequisites(
        self, request: DrillRequest, baseline: RestoreBaseline,
    ) -> RestorePlan:
        try:
            plan = build_restore_plan(
                account_id=request.account_id,
                environment=request.environment,
                projector_version=request.projector_version,
                backup_label=request.backup_label,
                target_time=request.target_time,
                run_id=self._run_id_factory(),
                database_name=baseline.database_name,
                expected_event_hash=baseline.event_hash,
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

    def _write_env_file(self, plan: RestorePlan, password: str) -> Path:
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
            "BFX_DEPLOYMENT_ENV": plan.environment,
            "DR_ACCOUNT_ID": plan.account_id,
            "DR_PROJECTOR_VERSION": plan.projector_version,
            "DR_TARGET_BACKUP_LABEL": plan.backup_label,
            "DR_TARGET_TIME": plan.target_time or "",
            "DR_PGBACKREST_SECRET_DIR": str(self._secret_dir),
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="bfx-dr-", suffix=".env", delete=False,
        ) as handle:
            path = Path(handle.name)
            os.chmod(path, 0o600)
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
        return path

    def _require_internal_network(self, plan: RestorePlan) -> None:
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
        self, command: tuple[str, ...], plan: RestorePlan
    ) -> None:
        completed = self._require_success(command)
        try:
            networks = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            _failure("restore_output_invalid")
        if not isinstance(networks, dict) or set(networks) != {plan.network_name}:
            _failure("restore_output_invalid")

    def _wait_for_recovery(self, plan: RestorePlan) -> None:
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

    def _wait_for_health(self, plan: RestorePlan) -> None:
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
            self._sleep(min(1, remaining))

    def _elapsed_seconds(self, started: float) -> int:
        elapsed_seconds = math.ceil(self._clock() - started)
        if elapsed_seconds < 0 or elapsed_seconds > _MAX_RTO_SECONDS:
            _failure("rto_invalid")
        return elapsed_seconds

    def _schema_tsv(self, plan: RestorePlan) -> str:
        query = (
            "SELECT current_setting('server_version_num'), "
            "(SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version), "
            "(SELECT count(*) FROM public.event_log "
            f"WHERE exchange_account_id = '{plan.account_id}'::uuid "
            f"AND deployment_environment = '{plan.environment}');\n"
        )
        completed = self._require_success(
            (
                "docker", "exec", "--user", "postgres", "--interactive", plan.container_name,
                "psql", "-X", "-qAt", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", plan.sql_admin_role, "-d", plan.database_name,
            ),
            input_text=query,
        )
        return completed.stdout

    def _bootstrap_role(self, plan: RestorePlan, password: str) -> None:
        # Connecting to the baseline database validates it exists before any SQL.
        # Send separate statements via psql stdin, with logging disabled before
        # the password-bearing statement is parsed/executed (including on error).
        tables = (
            "event_log", "offer_claims", "position_state", "venue_offer_state",
            "venue_credit_state", "projection_heads", "reconcile_observation",
            "submission_attempts", "execution_uncertainties", "alembic_version",
        )
        role = f'"{plan.verify_role}"'
        table_list = ", ".join(f'public."{table}"' for table in tables)
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
            f"GRANT SELECT ON TABLE {table_list} TO {role};\n"
            "DO $archive$ BEGIN IF EXISTS (SELECT FROM pg_namespace WHERE nspname='projection_audit') THEN "
            f"GRANT USAGE ON SCHEMA projection_audit TO {role}; "
            f"GRANT SELECT ON TABLE projection_audit.runs, projection_audit.rows TO {role}; "
            "END IF; END $archive$;\n"
            "COMMIT;\n"
        )
        self._require_success(
            (
                "docker", "exec", "--user", "postgres", "--interactive", plan.container_name,
                "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", plan.sql_admin_role, "-d", plan.database_name,
            ),
            input_text=sql,
        )

    def _replay_json(self, command: tuple[str, ...], plan: RestorePlan) -> str:
        completed = self._require_success(command)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            _failure("restore_output_invalid")
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError:
            _failure("restore_output_invalid")
        if not isinstance(payload, dict):
            _failure("restore_output_invalid")
        if (
            payload.get("account_id") != plan.account_id
            or payload.get("environment") != plan.environment
            or payload.get("projector_version") != plan.projector_version
        ):
            _failure("restore_output_invalid")
        event_hash = payload.get("event_hash")
        if not isinstance(event_hash, str) or _REPLAY_HASH.fullmatch(event_hash) is None:
            _failure("event_hash_invalid")
        diagnostics = payload.get("diagnostic_diff")
        if not isinstance(diagnostics, dict) or any(
            not isinstance(value, dict) or value.get("matches") is not True
            for value in diagnostics.values()
        ):
            _failure("projection_replay_mismatch")
        return lines[0]

    def _image_metadata(self, plan: RestorePlan) -> tuple[str, dict[str, str]]:
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
        plan: RestorePlan,
        env_path: Path | None,
        *,
        container_started: bool,
        verifier_started: bool,
        volume_created: bool,
        egress_network_created: bool,
        network_created: bool,
        archive_path: Path | None = None,
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
        if archive_path is not None:
            try:
                _unlink_env_file(archive_path, timeout=self._remaining())
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

    def run(self, request: DrillRequest) -> int:
        plan: RestorePlan | None = None
        env_path: Path | None = None
        archive_path: Path | None = None
        timing_output_path = self._output_path.with_name(
            f"{self._output_path.stem}-timing.json"
        )
        timing = StageTiming(clock=self._clock)
        timing_stage: str | None = None
        timing_usable = True
        network_created = False
        egress_network_created = False
        volume_created = False
        container_cleanup_eligible = False
        verifier_cleanup_eligible = False
        rto_started: float | None = None
        failure_code: str | None = None
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
                baseline = load_restore_baseline(
                    request.baseline_path, target_backup_label=request.backup_label,
                    target_time=request.target_time, account_id=request.account_id,
                    environment=request.environment, projector_version=request.projector_version,
                )
                plan = self._validate_prerequisites(request, baseline)
                if type(request.archive_only) is not bool or (request.archive_only and not baseline.archives):
                    _failure("restore_output_invalid")
                if request.archive_only != (request.target_run_id is not None):
                    _failure("restore_output_invalid")
                _evidence._archive.validate_target(request.target_run_id, baseline.archives or ())
                password = self._password_factory()
                if not isinstance(password, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password) is None:
                    _failure("restore_output_invalid")
                env_path = self._write_env_file(plan, password)
                rto_started = self._clock()
                self._deadline = rto_started + _MAX_RTO_SECONDS
                begin_timing("resource_setup")
                # Archive preparation/verification stays inside the existing deadline.
                transport = _evidence._archive.transport(baseline.archives or (), target_run_id=request.target_run_id)
                with tempfile.NamedTemporaryFile(prefix="bfx-dr-archive-", suffix=".json", delete=False) as handle:
                    archive_path = Path(handle.name)
                    os.chmod(archive_path, 0o600)
                    handle.write(transport)
                verifier_image = self._require_success((
                    "docker", "image", "inspect", "--format={{.Id}}", "bfx-bot:local",
                )).stdout.strip()
                if not _evidence._archive.image_digest(verifier_image) or (
                    baseline.verifier_image_digest is not None and baseline.verifier_image_digest != verifier_image
                ):
                    _failure("restore_output_invalid")
                self._require_success(plan.create_commands[0])
                network_created = True
                self._require_internal_network(plan)
                self._require_success(plan.create_commands[1])
                egress_network_created = True
                self._require_success(plan.create_commands[2])
                volume_created = True
                end_timing("resource_setup")
                begin_timing("physical_and_wal_recovery")
                container_cleanup_eligible = True
                self._require_success(_compose_with_env(plan.run_commands[0], env_path))
                self._wait_for_health(plan)
                self._wait_for_recovery(plan)
                end_timing("physical_and_wal_recovery")
                begin_timing("isolation_bootstrap")
                self._require_external_egress(plan.run_commands[1])
                self._require_success(plan.run_commands[2])
                self._require_egress_absent(plan.run_commands[3], plan)
                self._bootstrap_role(plan, password)
                schema_tsv = self._schema_tsv(plan)
                end_timing("isolation_bootstrap")
                begin_timing("verification")
                verifier_cleanup_eligible = True
                archive_json = self._require_success(_commands.verifier_command(
                    plan, image=verifier_image, env_path=env_path, input_path=archive_path,
                    input_digest=hashlib.sha256(transport).hexdigest(), archive_only=request.archive_only,
                )).stdout
                _evidence._archive.validate_report(archive_json, baseline=baseline, archive_only=request.archive_only,
                                                   target_run_id=request.target_run_id)
                replay_json = ""
                if not request.archive_only:
                    replay_json = self._replay_json(_commands.verifier_command(
                        plan, image=verifier_image, env_path=env_path,
                    ), plan)
                self._remaining()
                if not request.archive_only:
                    _evidence.validate_restore_state(
                        schema_tsv=schema_tsv, replay_json=replay_json, baseline=baseline,
                    )
                image_digest, image_labels = self._image_metadata(plan)
                if rto_started is None:
                    _failure("rto_invalid")
                elapsed_seconds = self._elapsed_seconds(rto_started)
                self._remaining()
                success_report = render_restore_evidence(
                    schema_tsv=schema_tsv,
                    replay_json=replay_json,
                    baseline=baseline,
                    observed_at_ms=time.time_ns() // 1_000_000,
                    egress_disconnected=True,
                    elapsed_seconds=elapsed_seconds,
                    config_path=self._config_path,
                    image_digest=image_digest,
                    image_labels=image_labels,
                    network_name=plan.network_name,
                    network_internal=True,
                    archive_json=archive_json, archive_only=request.archive_only,
                    target_run_id=request.target_run_id,
                    verifier_image_digest=verifier_image,
                )
                success_report["rto_seconds"] = self._elapsed_seconds(rto_started)
                success_report["restore_run_id"] = plan.project_name.removeprefix("bfx-dr-")
                success_report["archive_input_digest"] = hashlib.sha256(transport).hexdigest()
                self._remaining()
                end_timing("verification")
        except DrillFailureError as exc:
            failure_code = str(exc)
        except EvidenceError as exc:
            failure_code = str(exc)
        except (OSError, ValueError, TypeError):
            failure_code = "restore_output_invalid"
        finally:
            if timing_stage is not None:
                end_timing(timing_stage)
            if plan is not None:
                begin_timing("cleanup")
                cleanup_failed = self._cleanup(
                    plan, env_path, container_started=container_cleanup_eligible,
                    verifier_started=verifier_cleanup_eligible,
                    volume_created=volume_created,
                    egress_network_created=egress_network_created,
                    network_created=network_created,
                    archive_path=archive_path,
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
                try:
                    report = render_failure_evidence(
                        kind="archive_restore" if request.archive_only else "restore", error_code=failure_code,
                        observed_at_ms=time.time_ns() // 1_000_000,
                    )
                except EvidenceError:
                    report = render_failure_evidence(
                        kind="archive_restore" if request.archive_only else "restore", error_code="restore_output_invalid",
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
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--projector-version", required=True)
    parser.add_argument("--backup-label", required=True)
    parser.add_argument("--target-time")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--archive-only", action="store_true")
    parser.add_argument("--target-run-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return RestoreDrill().run(
        DrillRequest(
            account_id=args.account_id,
            environment=args.environment,
            projector_version=args.projector_version,
            backup_label=args.backup_label,
            target_time=args.target_time,
            baseline_path=args.baseline,
            archive_only=args.archive_only,
            target_run_id=args.target_run_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
