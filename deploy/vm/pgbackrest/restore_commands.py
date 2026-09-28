"""Validated argv-only Docker commands for an isolated restore drill."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from uuid import UUID

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.dr.yml"
_ENVIRONMENTS = frozenset({"prod", "shadow", "ci"})
_PROJECTOR_VERSION = re.compile(r"[A-Za-z0-9._-]+")
_BACKUP_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_RUN_ID = re.compile(r"[0-9TZ-]+-[a-f0-9]{16}")
_TARGET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_GENERATED_NAME = re.compile(r"bfx-dr-[a-z0-9-]+")
_VERIFY_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_DATABASE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_EVENT_HASH = re.compile(r"[0-9a-f]{64}")
_BACKEND_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class RestoreInputError(ValueError):
    """Restore input is malformed or would escape the generated namespace."""


@dataclass(frozen=True, slots=True)
class RestoreResources:
    project_name: str
    volume_name: str
    network_name: str
    egress_network_name: str
    container_name: str
    verifier_container_name: str
    sql_admin_role: str
    verify_role: str
    database_name: str
    backup_label: str
    target_time: str | None
    create_commands: tuple[tuple[str, ...], ...]
    run_commands: tuple[tuple[str, ...], ...]
    cleanup_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class RestorePlan(RestoreResources):
    """A scoped archive/prefix verifier layered on the restore resources."""

    account_id: str
    environment: str
    projector_version: str
    # None only in prefix mode, which has no baseline hash.
    expected_event_hash: str | None


def _invalid() -> None:
    raise RestoreInputError("invalid_restore_input")


def verifier_command(
    plan: RestorePlan, *, image: str, env_path: Path, input_path: Path | None = None,
    input_digest: str | None = None, archive_only: bool = False,
) -> tuple[str, ...]:
    """Pin both verifiers to the observed bot image on the isolated network."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None or not env_path.is_absolute():
        _invalid()
    command = ("docker", "run", "--rm", "--name", plan.verifier_container_name,
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--network", plan.network_name, "--env-file", str(env_path), "--entrypoint", "python")
    if input_path is not None:
        if (not input_path.is_absolute() or any(c in str(input_path) for c in ":,\n\r")
                or input_digest is None or _EVENT_HASH.fullmatch(input_digest) is None):
            _invalid()
        return (*command, "--volume", f"{input_path}:/run/archive-input.json:ro", image,
                "scripts/verify_projection_archive.py", "--input", "/run/archive-input.json",
                "--input-digest", input_digest, "--account-id", plan.account_id,
                "--environment", plan.environment, *(("--archive-only",) if archive_only else ()))
    if plan.expected_event_hash is None:
        _invalid()
    return (*command, image, "scripts/verify_projection_replay.py", "replay",
            "--account-id", plan.account_id, "--environment", plan.environment,
            "--projector-version", plan.projector_version, "--expected-event-hash", plan.expected_event_hash)


def prefix_verifier_command(plan: RestorePlan, *, image: str, env_path: Path) -> tuple[str, ...]:
    """Prefix mode: run prefix_verify.py (fed on stdin) in the observed bot image.

    Same container name, user, isolated network and env file as the replay
    verifier, so cleanup and isolation are unchanged; `-i` carries the script.
    """
    if (re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None or not env_path.is_absolute()
            or plan.expected_event_hash is not None):
        _invalid()
    return ("docker", "run", "--rm", "-i", "--name", plan.verifier_container_name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", plan.network_name, "--env-file", str(env_path), "--entrypoint", "python",
            image, "-", "--account-id", plan.account_id, "--environment", plan.environment,
            "--projector-version", plan.projector_version)


def rehearsal_command(
    plan: RestoreResources, *, image: str, code_revision: str, dsn_path: Path,
    manifest_path: Path, cells_path: Path, scopes: tuple[str, ...],
) -> tuple[str, ...]:
    """Run the fixed comparison command with only three read-only input files."""
    validate_rehearsal_image(image)
    if _REVISION.fullmatch(code_revision) is None:
        _invalid()
    validate_rehearsal_scopes(scopes)
    for path in (dsn_path, manifest_path, cells_path):
        if not path.is_absolute() or any(character in str(path) for character in ":\n\r"):
            _invalid()
    run_id = plan.project_name.removeprefix("bfx-dr-")
    return (
        "docker", "run", "--rm", "--pull", "never", "--name", plan.verifier_container_name,
        "--label", "autoheal=false", "--user", f"{os.getuid()}:{os.getgid()}",
        "--network", plan.network_name, "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--pids-limit", "256",
        "--volume", f"{dsn_path}:/run/bfx-comparison/dsn:ro",
        "--volume", f"{manifest_path}:/run/bfx-comparison/manifest.json:ro",
        "--volume", f"{cells_path}:/run/bfx-comparison/cells.yaml:ro",
        "--workdir", "/app", "--entrypoint", "python", image,
        "-m", "bfx_funding_bot.apps.capital_comparison", "--mode", "rehearsal",
        "--dsn-file", "/run/bfx-comparison/dsn", "--manifest", "/run/bfx-comparison/manifest.json",
        "--run-id", run_id, "--code-revision", code_revision,
        "--cells", "/run/bfx-comparison/cells.yaml",
        *(part for scope in scopes for part in ("--scope", scope)),
    )


def validate_rehearsal_image(image: str) -> None:
    if not isinstance(image, str) or _BACKEND_IMAGE.fullmatch(image) is None:
        _invalid()


def validate_rehearsal_scopes(scopes: tuple[str, ...]) -> None:
    if not scopes:
        _invalid()
    for scope in scopes:
        if not isinstance(scope, str):
            _invalid()
        try:
            account, environment = scope.split(":")
        except ValueError:
            _invalid()
        if _canonical_account_id(account) != account or environment not in _ENVIRONMENTS:
            _invalid()


def _canonical_account_id(account_id: str) -> str:
    try:
        canonical = str(UUID(account_id))
    except (TypeError, ValueError):
        _invalid()
    if account_id != canonical:
        _invalid()
    return canonical


def _generated_name(value: str) -> str:
    if _GENERATED_NAME.fullmatch(value) is None:
        _invalid()
    return value


def _verify_role(value: str) -> str:
    if _VERIFY_ROLE.fullmatch(value) is None:
        _invalid()
    return value


def build_restore_resources(
    *, backup_label: str, target_time: str | None, run_id: str, database_name: str,
) -> RestoreResources:
    """Build generated resources without assuming a verification scope."""
    if not isinstance(database_name, str) or _DATABASE_NAME.fullmatch(database_name) is None:
        _invalid()
    if not isinstance(backup_label, str) or _BACKUP_LABEL.fullmatch(backup_label) is None:
        _invalid()
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        _invalid()
    if target_time is not None and (
        not isinstance(target_time, str) or _TARGET_TIME.fullmatch(target_time) is None
    ):
        _invalid()

    resource_id = run_id.lower()
    project_name = _generated_name(f"bfx-dr-{resource_id}")
    volume_name = _generated_name(f"bfx-dr-{resource_id}-data")
    network_name = _generated_name(f"bfx-dr-{resource_id}-net")
    egress_network_name = _generated_name(f"bfx-dr-{resource_id}-egress")
    container_name = _generated_name(f"bfx-dr-{resource_id}-db")
    verifier_container_name = _generated_name(f"bfx-dr-{resource_id}-verifier")
    verify_role = _verify_role(f"bfx_dr_{resource_id.replace('-', '_')}")
    compose_prefix = (
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_PATH),
    )
    return RestoreResources(
        project_name=project_name,
        volume_name=volume_name,
        network_name=network_name,
        egress_network_name=egress_network_name,
        container_name=container_name,
        verifier_container_name=verifier_container_name,
        sql_admin_role="bfx",
        verify_role=verify_role,
        database_name=database_name,
        backup_label=backup_label,
        target_time=target_time,
        create_commands=(
            ("docker", "network", "create", "--internal", network_name),
            ("docker", "network", "create", egress_network_name),
            ("docker", "volume", "create", volume_name),
        ),
        run_commands=(
            (*compose_prefix, "up", "--detach", "restore-db"),
            (
                "docker",
                "network",
                "inspect",
                "--format={{.Internal}}",
                egress_network_name,
            ),
            (
                "docker",
                "network",
                "disconnect",
                egress_network_name,
                container_name,
            ),
            (
                "docker",
                "inspect",
                "--format={{json .NetworkSettings.Networks}}",
                container_name,
            ),
        ),
        cleanup_commands=(
            (*compose_prefix, "rm", "-sf", "restore-db"),
            ("docker", "volume", "rm", volume_name),
            ("docker", "network", "rm", egress_network_name),
            ("docker", "network", "rm", network_name),
            ("docker", "container", "rm", "--force", verifier_container_name),
        ),
    )


def build_restore_plan(
    *,
    account_id: str,
    environment: str,
    projector_version: str,
    backup_label: str,
    target_time: str | None,
    run_id: str,
    database_name: str,
    expected_event_hash: str | None,
) -> RestorePlan:
    """Layer the legacy single-scope verifier on generated restore resources."""
    canonical_account_id = _canonical_account_id(account_id)
    if environment not in _ENVIRONMENTS or not isinstance(projector_version, str) \
            or _PROJECTOR_VERSION.fullmatch(projector_version) is None:
        _invalid()
    if expected_event_hash is not None and (
        not isinstance(expected_event_hash, str) or _EVENT_HASH.fullmatch(expected_event_hash) is None
    ):
        _invalid()
    resources = build_restore_resources(
        backup_label=backup_label, target_time=target_time,
        run_id=run_id, database_name=database_name,
    )
    compose_prefix = (
        "docker", "compose", "--project-name", resources.project_name,
        "--file", str(COMPOSE_PATH),
    )
    verifier_run = (
        *compose_prefix, "run", "--rm", "--no-deps", "--name",
        resources.verifier_container_name, "verifier", "replay",
        "--account-id", canonical_account_id, "--environment", environment,
        "--projector-version", projector_version,
        *(("--expected-event-hash", expected_event_hash)
          if expected_event_hash is not None else ()),
    )
    values = {field.name: getattr(resources, field.name) for field in fields(RestoreResources)}
    values["run_commands"] = (*resources.run_commands, verifier_run)
    return RestorePlan(
        **values, account_id=canonical_account_id, environment=environment,
        projector_version=projector_version, expected_event_hash=expected_event_hash,
    )
