"""Read-only local operational checks; not proof against unmanaged external writers.

Fixed Docker/systemd inventory covers the tracked deployment's restart sources.
DB session inspection and table locks must additionally fence capture. An external
administrator can still start a new writer; this tooling grants no resume authority.
Runtime code must inject a typed verifier instead of importing this operator script.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.projection_cutover.archive import audit_runtime_roles
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope

Runner = Callable[[tuple[str, ...]], Awaitable[bytes]]
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
SYSTEMCTL = ("/usr/bin/systemctl", "--no-pager", "--no-legend")
SERVICES = frozenset({"bot", "webapi", "frontend", "autoheal", "migrate", "weekly-report", "postgres", "redis"})
REQUIRED = frozenset({"bot", "webapi", "frontend", "autoheal"})
RECREATE_UNITS = frozenset({
    "bfx-weekly-report.service", "bfx-weekly-report.timer",
    "bfx-halt-watch.service", "bfx-halt-watch.timer",
})
OPTIONAL_WRITERS = frozenset({"bfx-l3-verify-24h.service"})
# These tracked units run pgBackRest/status only, not execution projections.
# Keep backup scheduling active; the quiescence boundary must not relax RPO.
BACKUP_UNITS = frozenset({
    # Legacy daily pg_dump remains a backup-only reader and is allowed to stay
    # enabled during the cutover quiescence window.
    "bfx-pg-backup.service", "bfx-pg-backup.timer",
    "bfx-pgbackrest-backup.service", "bfx-pgbackrest-backup.timer",
    "bfx-pgbackrest-status.service", "bfx-pgbackrest-status.timer",
})
INSPECT = ('{"id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
           '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
           '"image":{{json .Image}},"running":{{json .State.Running}},'
           '"paused":{{json .State.Paused}},"restarting":{{json .State.Restarting}},'
           '"status":{{json .State.Status}},"restart":{{json .HostConfig.RestartPolicy.Name}}}')


async def run_fixed(argv: tuple[str, ...]) -> bytes:
    """Only called with this module's constructed argv; bounded time and stdout."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            assert process.stdout is not None
            result = await process.stdout.read(65537)
            if len(result) > 65536:
                raise ValueError("operational_output_limit")
            # read(n) can return short before EOF: continue without exceeding cap.
            while chunk := await process.stdout.read(65537 - len(result)):
                result += chunk
                if len(result) > 65536:
                    raise ValueError("operational_output_limit")
            if await process.wait() != 0:
                raise ValueError("operational_probe_failed")
            return result
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verify_local_operations(
    expected: Mapping[str, Any], *, runner: Runner = run_fixed,
    environ: Mapping[str, str] | None = None,
) -> None:
    env = os.environ if environ is None else environ
    if (env.get("DOCKER_HOST", "unix:///var/run/docker.sock") != "unix:///var/run/docker.sock"
            or env.get("DOCKER_CONTEXT", "default") != "default"
            or any(env.get(key) for key in ("DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"))):
        raise ValueError("docker_endpoint_not_local")
    if (expected.get("project") != "bfx" or not expected.get("daemon_id")
            or set(expected.get("images", {})) != SERVICES
            or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
                   for image in expected["images"].values())):
        raise ValueError("operational_identity_invalid")
    try:
        async with asyncio.timeout(30):
            async def read(argv: tuple[str, ...]) -> str:
                output = await runner(argv)
                if len(output) > 65536:
                    raise ValueError("operational_output_limit")
                return output.decode("utf-8").strip()

            endpoint = json.loads(await read(("/usr/bin/docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")))
            if endpoint != "unix:///var/run/docker.sock":
                raise ValueError("docker_context_not_local")
            if json.loads(await read((*DOCKER, "info", "--format", "{{json .ID}}"))) != expected["daemon_id"]:
                raise ValueError("docker_daemon_mismatch")
            ids = (await read((*DOCKER, "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=bfx"))).splitlines()
            if not ids or len(ids) > 64 or len(set(ids)) != len(ids):
                raise ValueError("container_inventory_invalid")
            found = set()
            for identity in ids:
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    raise ValueError("container_identity_invalid")
                state = json.loads(await read((*DOCKER, "inspect", "--format", INSPECT, identity)))
                service = state.get("service")
                # Compose 5.x omits the oneoff label on persistent containers;
                # Docker's Go template therefore returns JSON null.  Only the
                # absent-label case maps to the persistent value.  Explicit
                # non-boolean labels remain invalid and fail closed.
                if state.get("oneoff") is None:
                    state["oneoff"] = "False"
                if (state.get("id") != identity or state.get("project") != "bfx"
                        or service not in SERVICES or state.get("oneoff") not in {"True", "False"}
                        or state.get("image") != expected["images"][service]):
                    raise ValueError("container_inventory_mismatch")
                if state["oneoff"] == "False":
                    found.add(service)
                if service not in {"postgres", "redis"} and (
                    any(state.get(key) is not False for key in ("running", "paused", "restarting"))
                    or state.get("status") not in {"exited", "created"}
                    or state.get("restart") != "no"
                ):
                    raise ValueError("container_writer_not_quiescent")
            if not found >= REQUIRED:
                raise ValueError("container_inventory_missing")
            known = RECREATE_UNITS | OPTIONAL_WRITERS | BACKUP_UNITS
            unit_files = (await read((*SYSTEMCTL, "list-unit-files", "bfx*"))).splitlines()
            loaded = (await read((*SYSTEMCTL, "list-units", "--all", "--plain", "bfx*"))).splitlines()
            units = {line.split()[0] for line in [*unit_files, *loaded] if line.strip()}
            if not units >= RECREATE_UNITS or not units <= known:
                raise ValueError("project_unit_inventory_mismatch")
            for unit in sorted(units - BACKUP_UNITS):
                value = await read((*SYSTEMCTL, "show", unit,
                    "--property=LoadState,ActiveState,SubState,UnitFileState,Job"))
                state = dict(line.split("=", 1) for line in value.splitlines())
                if (state.get("LoadState") != "masked" or state.get("ActiveState") != "inactive"
                        or state.get("SubState") != "dead" or state.get("UnitFileState") != "masked"
                        or state.get("Job") not in {"", "0"}):
                    raise ValueError("recreate_unit_not_quiescent")
    except Exception:
        raise ValueError("operational_quiescence_blocked") from None


async def verify_database_quiescence(
    session: AsyncSession, *, scope: Scope, runtime_roles: tuple[str, ...],
) -> None:
    """SELECT-only checks under caller's locks, with no SQL text inspection.

    Include idle runtime sessions: they can issue another command. Other client
    transactions also block. Idle operator pool connections remain permissible;
    table locks fence their writes during capture, not after the transaction.
    Each call discards the connection's cached activity snapshot; otherwise a
    second check in this same transaction could miss a newly connected writer.
    """
    await audit_runtime_roles(session, role_names=runtime_roles)
    omitted = await session.scalar(text("""
        WITH RECURSIVE reachable(login_oid, role_oid) AS (
          SELECT oid,oid FROM pg_roles WHERE rolcanlogin AND rolname<>session_user
          UNION
          SELECT r.login_oid,m.roleid FROM reachable r JOIN pg_auth_members m ON m.member=r.role_oid
        )
        SELECT EXISTS (
          SELECT 1 FROM reachable x JOIN pg_roles login ON login.oid=x.login_oid
          JOIN pg_roles r ON r.oid=x.role_oid
          WHERE NOT (login.rolname = ANY(CAST(:roles AS text[])))
          AND (r.rolsuper OR has_schema_privilege(r.oid,'public','CREATE') OR EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')
            AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE')
                 OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE'))
          ))
        )
    """), {"roles": list(runtime_roles)})
    await session.execute(text("SELECT pg_stat_clear_snapshot()"))
    active = await session.scalar(text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_stat_activity
          WHERE datname=current_database() AND pid<>pg_backend_pid()
          AND (backend_type='client backend' OR backend_type IS NULL)
          AND (usename=ANY(CAST(:roles AS text[])) OR state IS NULL
               OR state<>'idle' OR xact_start IS NOT NULL OR usename<>session_user)
        )
    """), {"roles": list(runtime_roles)})
    trading = await session.scalar(text("""
        SELECT state FROM public.trading_state WHERE exchange_account_id=:id
        AND deployment_environment=:env ORDER BY id DESC LIMIT 1
    """), {"id": scope.account_id, "env": scope.environment})
    if omitted or active or trading != "HALTED":
        raise ValueError("database_not_quiescent")
