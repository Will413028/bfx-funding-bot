"""Validated argv-only Docker commands for an isolated restore drill."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


class RestoreInputError(ValueError):
    """Restore input is malformed or would escape the generated namespace."""


@dataclass(frozen=True, slots=True)
class RestorePlan:
    project_name: str
    volume_name: str
    network_name: str
    egress_network_name: str
    container_name: str
    verifier_container_name: str
    sql_admin_role: str
    verify_role: str
    database_name: str
    account_id: str
    environment: str
    projector_version: str
    backup_label: str
    target_time: str | None
    expected_event_hash: str
    create_commands: tuple[tuple[str, ...], ...]
    run_commands: tuple[tuple[str, ...], ...]
    cleanup_commands: tuple[tuple[str, ...], ...]


def _invalid() -> None:
    raise RestoreInputError("invalid_restore_input")


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


def build_restore_plan(
    *,
    account_id: str,
    environment: str,
    projector_version: str,
    backup_label: str,
    target_time: str | None,
    run_id: str,
    database_name: str,
    expected_event_hash: str,
) -> RestorePlan:
    """Validate operator strings and return argv-safe Docker commands."""
    canonical_account_id = _canonical_account_id(account_id)
    if not isinstance(database_name, str) or _DATABASE_NAME.fullmatch(database_name) is None:
        _invalid()
    if not isinstance(expected_event_hash, str) or _EVENT_HASH.fullmatch(expected_event_hash) is None:
        _invalid()
    if environment not in _ENVIRONMENTS:
        _invalid()
    if _PROJECTOR_VERSION.fullmatch(projector_version) is None:
        _invalid()
    if _BACKUP_LABEL.fullmatch(backup_label) is None:
        _invalid()
    if _RUN_ID.fullmatch(run_id) is None:
        _invalid()
    if target_time is not None and _TARGET_TIME.fullmatch(target_time) is None:
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
    return RestorePlan(
        project_name=project_name,
        volume_name=volume_name,
        network_name=network_name,
        egress_network_name=egress_network_name,
        container_name=container_name,
        verifier_container_name=verifier_container_name,
        sql_admin_role="bfx",
        verify_role=verify_role,
        database_name=database_name,
        expected_event_hash=expected_event_hash,
        account_id=canonical_account_id,
        environment=environment,
        projector_version=projector_version,
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
            (
                *compose_prefix,
                "run",
                "--rm",
                "--no-deps",
                "--name",
                verifier_container_name,
                "verifier",
                "replay",
                "--account-id",
                canonical_account_id,
                "--environment",
                environment,
                "--projector-version",
                projector_version,
                "--expected-event-hash",
                expected_event_hash,
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
