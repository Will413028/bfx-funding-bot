"""Validated argv-only Docker commands for an isolated restore drill."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.dr.yml"
_BACKUP_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_RUN_ID = re.compile(r"[0-9TZ-]+-[a-f0-9]{16}")
_TARGET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_GENERATED_NAME = re.compile(r"bfx-dr-[a-z0-9-]+")
_VERIFY_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_DATABASE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


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


def _invalid() -> None:
    raise RestoreInputError("invalid_restore_input")


# The image's own read-only boot check (``apps/restore_boot_check.py``; every deployed image has
# it since S1-8 PR-C), so the checks always match the API of the image they judge.
BOOT_CHECK_MODULE = "bfx_funding_bot.apps.restore_boot_check"


def ledger_verifier_command(
    resources: RestoreResources, *, image: str, env_path: Path,
) -> tuple[str, ...]:
    """Run the image's boot check module in the observed bot image.

    The generated verifier container name (cleaned up by name), the caller's uid, the
    isolated network and the env file (the restored cluster's read-only DATABASE_URL).
    """
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None or not env_path.is_absolute():
        _invalid()
    return ("docker", "run", "--rm", "--name", resources.verifier_container_name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", resources.network_name, "--env-file", str(env_path),
            "--entrypoint", "python", image, "-m", BOOT_CHECK_MODULE)


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
