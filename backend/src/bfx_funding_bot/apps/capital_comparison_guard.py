"""Explicit endpoint attestation; never loads settings or environment files.

Manifest JSON: mode, host, port (integer), database, user, run_id, and
now_ms (integer recovery-target clock, rehearsal only). Optional
``policy_without_cell``: a list of ``{account_id, environment, symbol}`` policy heads
that deliberately have no configured cell. Launcher mounts it read-only.
Cutover uses its own manifest and a command-start wall clock.

``user`` is the LOGIN (``session_user``). The tool then runs ``SET LOCAL ROLE
bfx_cutover_reader`` and attests the reachable role set and its privileges
(``verify_connection``); the runbook attestation lists the same checks.
"""

import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession

READER_ROLE: Final = "bfx_cutover_reader"
FORBIDDEN_EXTENSIONS: Final = ("dblink", "postgres_fdw")
_PRIVILEGED_ATTRIBUTES: Final = (
    "rolsuper",
    "rolcreaterole",
    "rolcreatedb",
    "rolbypassrls",
    "rolreplication",
)
_DETAIL_LIMIT: Final = 20


class GuardRejectedError(ValueError):
    """Only controlled reason codes (and catalog object names) cross the command boundary."""

    def __init__(self, reason: str, *detail: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ConnectionPlan:
    url: URL
    user: str
    database: str
    run_id: str
    now_ms: int
    mode: str
    policy_without_cell: frozenset[tuple[UUID, str, str]] = field(default_factory=frozenset)


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
    *,
    mode: str,
    dsn: str,
    manifest_path: Path | None,
    cutover_manifest_path: Path | None,
    run_id: str,
    authorize_cutover_read: bool,
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
        if url.username == READER_ROLE:
            raise GuardRejectedError("login_is_reader")
        if mode == "rehearsal":
            if manifest["host"] != f"bfx-dr-{run_id}-db" or manifest["port"] != 5432:
                raise ValueError
            now_ms = manifest.get("now_ms")
        else:
            now_ms = wall_clock_ms
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError
        return ConnectionPlan(
            url.set(drivername="postgresql+asyncpg"),
            url.username,
            url.database,
            run_id,
            now_ms,
            mode,
            _policy_without_cell(manifest.get("policy_without_cell")),
        )
    except GuardRejectedError:
        raise
    except Exception:
        raise GuardRejectedError("invalid_connection_manifest") from None


def _policy_without_cell(value: object) -> frozenset[tuple[UUID, str, str]]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError
    declared = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"account_id", "environment", "symbol"}:
            raise ValueError
        environment, symbol = item["environment"], item["symbol"]
        if environment not in {"prod", "shadow", "ci"} or not isinstance(symbol, str) or not symbol:
            raise ValueError
        declared.add((UUID(item["account_id"]), environment, symbol))
    return frozenset(declared)


# The roles reachable from the LOGIN through pg_auth_members, whatever the INHERIT/SET flags.
_CLOSURE = (
    "WITH RECURSIVE closure(oid) AS ("
    "SELECT r.oid FROM pg_roles r WHERE r.rolname = session_user "
    "UNION SELECT m.roleid FROM pg_auth_members m JOIN closure c ON m.member = c.oid) "
)
# Every non-system schema is scanned (``auth`` holds the web sessions and roles, so a write
# there forges the operator path); TEMP is deliberately not checked (restore drill grants it
# and a temp object cannot reach another session).
_NOT_SYSTEM = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'"
)
_SCANNED = _NOT_SYSTEM + " "
_RELATIONS = "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace CROSS JOIN closure r "
_PRIVILEGE_SCANS: tuple[tuple[str, str], ...] = (
    (
        "table_write_privilege",
        "SELECT DISTINCT n.nspname || '.' || c.relname "
        + _RELATIONS
        + "WHERE "
        + _SCANNED
        + "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND has_table_privilege(r.oid, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,MAINTAIN')",
    ),
    (
        "column_write_privilege",
        "SELECT DISTINCT n.nspname || '.' || c.relname "
        + _RELATIONS
        + "WHERE "
        + _SCANNED
        + "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND has_any_column_privilege(r.oid, c.oid, 'INSERT,UPDATE')",
    ),
    (
        "sequence_privilege",
        "SELECT DISTINCT n.nspname || '.' || c.relname "
        + _RELATIONS
        + "WHERE "
        + _SCANNED
        + "AND c.relkind = 'S' "
        "AND has_sequence_privilege(r.oid, c.oid, 'USAGE,UPDATE')",
    ),
    (
        "database_create_privilege",
        "SELECT DISTINCT current_database() FROM closure r "
        "WHERE has_database_privilege(r.oid, current_database(), 'CREATE')",
    ),
    (
        "schema_create_privilege",
        "SELECT DISTINCT n.nspname FROM pg_namespace n CROSS JOIN closure r WHERE "
        + _NOT_SYSTEM
        + " AND has_schema_privilege(r.oid, n.oid, 'CREATE')",
    ),
    (
        "owned_relation",
        "SELECT DISTINCT n.nspname || '.' || c.relname " + _RELATIONS + "WHERE c.relowner = r.oid",
    ),
    (
        "owned_function",
        "SELECT DISTINCT n.nspname || '.' || p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace JOIN closure r ON p.proowner = r.oid",
    ),
    (
        "owned_schema",
        "SELECT DISTINCT n.nspname FROM pg_namespace n JOIN closure r ON n.nspowner = r.oid",
    ),
    (
        "security_definer_executable",
        "SELECT DISTINCT n.nspname || '.' || p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace CROSS JOIN closure r "
        "WHERE p.prosecdef AND p.prorettype <> CAST('trigger' AS regtype) AND "
        + _NOT_SYSTEM
        + " AND has_function_privilege(r.oid, p.oid, 'EXECUTE')",
    ),
    (
        "forbidden_extension",
        "SELECT extname FROM pg_extension WHERE extname = ANY(CAST(:extensions AS text[]))",
    ),
)


async def verify_connection(session: AsyncSession, plan: ConnectionPlan) -> None:
    """Only attestation queries precede this gate; no authority data is read.

    Raises ``GuardRejectedError`` with one distinct reason per failed check.
    """
    identity = (
        await session.execute(
            text(
                "SELECT session_user, current_database(), "
                "current_setting('transaction_isolation'), current_setting('transaction_read_only')"
            )
        )
    ).one()
    if tuple(identity) != (plan.user, plan.database, "repeatable read", "on"):
        raise GuardRejectedError("connection_attestation_failed")
    if plan.user == READER_ROLE:
        raise GuardRejectedError("login_is_reader")
    try:
        await session.execute(text(f"SET LOCAL ROLE {READER_ROLE}"))
        current = await session.scalar(text("SELECT current_user"))
    except Exception:
        raise GuardRejectedError("reader_role_unavailable") from None
    if current != READER_ROLE:
        raise GuardRejectedError("reader_role_unavailable")
    roles = (
        await session.execute(
            text(
                _CLOSURE
                + "SELECT r.rolname, r.rolcanlogin, r.rolinherit, "
                + ", ".join(f"r.{a}" for a in _PRIVILEGED_ATTRIBUTES)
                + " FROM closure c JOIN pg_roles r ON r.oid = c.oid ORDER BY r.rolname"
            )
        )
    ).all()
    if {row[0] for row in roles} != {plan.user, READER_ROLE}:
        raise GuardRejectedError("role_closure_unexpected", *(row[0] for row in roles))
    privileged = [row[0] for row in roles if any(row[3:])]
    if privileged:
        raise GuardRejectedError("role_attribute_privileged", *privileged)
    by_name = {row[0]: row for row in roles}
    if by_name[READER_ROLE][1]:
        raise GuardRejectedError("reader_can_login", READER_ROLE)
    # Since PG16 inheritance is decided per grant; rolinherit only sets the default for new ones.
    inheriting_grant = await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
            "JOIN pg_roles l ON l.oid = m.member JOIN pg_roles g ON g.oid = m.roleid "
            "WHERE l.rolname = session_user AND g.rolname = :reader AND m.inherit_option)"
        ),
        {"reader": READER_ROLE},
    )
    if by_name[plan.user][2] or inheriting_grant:
        raise GuardRejectedError("login_inherits", plan.user)
    for reason, query in _PRIVILEGE_SCANS:
        parameters: dict[str, object] = {}
        if ":extensions" in query:
            parameters["extensions"] = list(FORBIDDEN_EXTENSIONS)
        found = list((await session.scalars(text(_CLOSURE + query), parameters)).all())
        if found:
            raise GuardRejectedError(reason, *found[:_DETAIL_LIMIT])
