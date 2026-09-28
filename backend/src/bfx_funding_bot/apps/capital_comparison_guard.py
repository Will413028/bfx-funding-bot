"""Explicit endpoint attestation; never loads settings or environment files.

Manifest JSON: mode, host, port (integer), database, user, run_id, and
now_ms (integer recovery-target clock, rehearsal only). Launcher mounts it read-only.
Cutover uses its own manifest and a command-start wall clock.
"""

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession

AUTHORITY_TABLES = (
    "event_log", "event_prefix_hashes", "capital_policy_heads", "capital_policy_revisions",
    "capital_snapshots", "capital_snapshot_queries", "execution_decisions", "projection_heads",
    "submission_attempts", "execution_uncertainties",
)


class GuardRejectedError(ValueError):
    """Only controlled reason codes may cross the command boundary."""


@dataclass(frozen=True)
class ConnectionPlan:
    url: URL
    user: str
    database: str
    run_id: str
    now_ms: int
    mode: str


def read_dsn(path: Path) -> str:
    """Read the DSN from an owner-only file so it never appears in argv."""
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise GuardRejectedError("dsn_file_permissions")
        lines = path.read_text().strip().splitlines()
    except GuardRejectedError:
        raise
    except OSError:
        raise GuardRejectedError("dsn_file_unreadable") from None
    if len(lines) != 1 or not lines[0].strip():
        raise GuardRejectedError("dsn_file_invalid")
    return lines[0].strip()


def validate_connection(
    *, mode: str, dsn: str, manifest_path: Path | None,
    cutover_manifest_path: Path | None, run_id: str, authorize_cutover_read: bool,
    wall_clock_ms: int,
) -> ConnectionPlan:
    if mode not in {"rehearsal", "cutover"}:
        raise GuardRejectedError("mode_required")
    if mode == "rehearsal":
        if authorize_cutover_read or cutover_manifest_path is not None or manifest_path is None:
            raise GuardRejectedError("rehearsal_manifest_required")
        path = manifest_path
    else:
        if not authorize_cutover_read or manifest_path is not None or cutover_manifest_path is None:
            raise GuardRejectedError("cutover_authorization_required")
        path = cutover_manifest_path
    try:
        manifest = json.loads(path.read_text())
        url = make_url(dsn)
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError
        if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError
        # Reject all URI options, including service, options, hostaddr and encoded overrides.
        if url.query or not url.host or any(c in url.host for c in ",/\\% \t\n"):
            raise ValueError
        if not url.username or not url.database or not url.port:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError
        if manifest.get("mode") != mode or manifest.get("run_id") != run_id:
            raise ValueError
        if type(manifest.get("port")) is not int:
            raise ValueError
        if url.host != manifest.get("host"):
            raise GuardRejectedError("host_mismatch")
        for actual, key in ((url.port, "port"), (url.database, "database"), (url.username, "user")):
            if actual != manifest.get(key):
                raise ValueError
        if mode == "rehearsal":
            if manifest["host"] != f"bfx-dr-{run_id}-db" or manifest["port"] != 5432:
                raise ValueError
            now_ms = manifest.get("now_ms")
        else:
            now_ms = wall_clock_ms
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError
        return ConnectionPlan(
            url.set(drivername="postgresql+asyncpg"), url.username, url.database,
            run_id, now_ms, mode,
        )
    except GuardRejectedError:
        raise
    except Exception:
        raise GuardRejectedError("invalid_connection_manifest") from None


async def verify_connection(session: AsyncSession, plan: ConnectionPlan) -> None:
    """Only attestation queries precede this gate; no authority data is read."""
    identity = (await session.execute(text(
        "SELECT current_user, current_database(), "
        "current_setting('transaction_isolation'), current_setting('transaction_read_only')"
    ))).one()
    if tuple(identity) != (plan.user, plan.database, "repeatable read", "on"):
        raise GuardRejectedError("connection_attestation_failed")
    for table in AUTHORITY_TABLES:
        if await session.scalar(text(
            "SELECT has_table_privilege(current_user, :table, 'INSERT,UPDATE,DELETE,TRUNCATE')"
        ), {"table": f"public.{table}"}):
            raise GuardRejectedError("authority_write_privilege")
