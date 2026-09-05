#!/usr/bin/env python3
"""Run a bounded, isolated pgBackRest restore drill without shell execution."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
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


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run_command(
    command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout, input=input_text
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


def _accepts_timeout(runner: CommandRunner) -> bool:
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        return runner is _run_command
    return any(
        parameter.name == "timeout"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _validate_plan_resources(plan: RestorePlan) -> None:
    if not all(
        _generated_resource(name)
        for name in (
            plan.project_name,
            plan.volume_name,
            plan.network_name,
            plan.egress_network_name,
            plan.container_name,
        )
    ):
        _failure("restore_output_invalid")


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


def _write_failure_log(output_path: Path, code: str) -> None:
    """Keep a bounded diagnostic marker without retaining command output."""
    log_path = output_path.with_name("restore.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"restore drill failed: {code}\n"[:8192], encoding="utf-8")
    os.chmod(log_path, 0o600)


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
        self._command_runner_accepts_timeout = _accepts_timeout(command_runner)

    def _call(
        self, command: tuple[str, ...], *, timeout: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            kwargs: dict[str, object] = {}
            if input_text is not None:
                kwargs["input_text"] = input_text
            if timeout is not None and self._command_runner_accepts_timeout:
                kwargs["timeout"] = timeout
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
        if not isinstance(networks, dict) or plan.egress_network_name in networks:
            _failure("restore_output_invalid")

    def _wait_for_health(self, plan: RestorePlan) -> None:
        deadline = self._clock() + 600
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
                "-h", "/var/run/postgresql", "-U", "postgres", "-d", plan.database_name,
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
            "COMMIT;\n"
        )
        self._require_success(
            (
                "docker", "exec", "--user", "postgres", "--interactive", plan.container_name,
                "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", "postgres", "-d", plan.database_name,
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

    def _image_digest(self) -> str:
        completed = self._require_success(
            ("docker", "image", "inspect", "--format={{index .RepoDigests 0}}", "bfx-postgres:local")
        )
        match = _SHA256.search(completed.stdout)
        if match is None:
            _failure("restore_output_invalid")
        return match.group(0)

    def _cleanup(
        self,
        plan: RestorePlan,
        env_path: Path | None,
        *,
        container_started: bool,
        volume_created: bool,
        egress_network_created: bool,
        network_created: bool,
    ) -> bool:
        failed = False
        commands: list[tuple[str, ...]] = []
        if container_started and env_path is not None:
            commands.append(_compose_with_env(plan.cleanup_commands[0], env_path))
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
            except DrillFailureError:
                failed = True
        return failed

    def run(self, request: DrillRequest) -> int:
        plan: RestorePlan | None = None
        env_path: Path | None = None
        network_created = False
        egress_network_created = False
        volume_created = False
        container_cleanup_eligible = False
        rto_started: float | None = None
        failure_code: str | None = None
        success_report: dict[str, object] | None = None
        success_persisted = False
        cleanup_failed = False
        failure_persist_failed = False
        try:
            baseline = load_restore_baseline(
                request.baseline_path, target_backup_label=request.backup_label,
                target_time=request.target_time, account_id=request.account_id,
                environment=request.environment, projector_version=request.projector_version,
            )
            plan = self._validate_prerequisites(request, baseline)
            password = self._password_factory()
            if not isinstance(password, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password) is None:
                _failure("restore_output_invalid")
            env_path = self._write_env_file(plan, password)
            self._require_success(plan.create_commands[0])
            network_created = True
            self._require_internal_network(plan)
            rto_started = self._clock()
            self._require_success(plan.create_commands[1])
            egress_network_created = True
            self._require_success(plan.create_commands[2])
            volume_created = True
            container_cleanup_eligible = True
            self._require_success(_compose_with_env(plan.run_commands[0], env_path))
            self._wait_for_health(plan)
            self._require_external_egress(plan.run_commands[1])
            self._require_success(plan.run_commands[2])
            self._require_egress_absent(plan.run_commands[3], plan)
            self._bootstrap_role(plan, password)
            schema_tsv = self._schema_tsv(plan)
            replay_json = self._replay_json(
                _compose_with_env(plan.run_commands[4], env_path), plan
            )
            image_digest = self._image_digest()
            if rto_started is None:
                _failure("rto_invalid")
            elapsed_seconds = self._elapsed_seconds(rto_started)
            success_report = render_restore_evidence(
                schema_tsv=schema_tsv,
                replay_json=replay_json,
                baseline=baseline,
                observed_at_ms=time.time_ns() // 1_000_000,
                egress_disconnected=True,
                elapsed_seconds=elapsed_seconds,
                config_path=self._config_path,
                image_digest=image_digest,
                network_name=plan.network_name,
                network_internal=True,
            )
            success_report["rto_seconds"] = self._elapsed_seconds(rto_started)
            _write_json(self._output_path, success_report)
            success_persisted = True
        except DrillFailureError as exc:
            failure_code = str(exc)
        except EvidenceError as exc:
            failure_code = str(exc)
        except (OSError, ValueError, TypeError):
            failure_code = "restore_output_invalid"
        finally:
            if plan is not None:
                cleanup_failed = self._cleanup(
                    plan, env_path, container_started=container_cleanup_eligible,
                    volume_created=volume_created,
                    egress_network_created=egress_network_created,
                    network_created=network_created,
                )
                if cleanup_failed and failure_code is None:
                    failure_code = "cleanup_failed"
            if env_path is not None:
                try:
                    env_path.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
                    if failure_code is None:
                        failure_code = "cleanup_failed"
            if success_persisted:
                if cleanup_failed:
                    try:
                        _write_failure_log(self._output_path, "cleanup_failed")
                    except OSError:
                        failure_persist_failed = True
            elif failure_code is not None:
                try:
                    report = render_failure_evidence(
                        kind="restore", error_code=failure_code,
                        observed_at_ms=time.time_ns() // 1_000_000,
                    )
                except EvidenceError:
                    report = render_failure_evidence(
                        kind="restore", error_code="restore_output_invalid",
                        observed_at_ms=time.time_ns() // 1_000_000,
                    )
                try:
                    _write_json(self._output_path, report)
                    _write_failure_log(
                        self._output_path,
                        "cleanup_failed" if cleanup_failed else str(report["error_code"]),
                    )
                except OSError:
                    failure_persist_failed = True
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
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
