"""The one-time legacy -> ledger closure seed (S1-4d). Dormant: an owner tool for the halt.

Never referenced by deploy, compose or systemd (``tests/architecture`` pins it). It refuses to
write unless every guard holds. On one connection, in this order:

1. before any transaction (autocommit): ``--authorize-seed`` and a manifest (mode ``seed``, run
   id, host, port, database, user, realm, scopes) naming exactly this DSN and these
   ``--scope`` arguments; the DSN's login is the ledger tables' owner, used directly (no SET
   ROLE), and none of the runtime roles (the schema also refuses seed rows from any other
   role); per scope, the daemon writer lock and the transaction writer lock are free and taken
   as session locks (held to the end); then no session of a runtime login (``bfx_bot``,
   ``bfx_webapi`` or a member of them) exists;
2. only then the one REPEATABLE READ transaction. Its first statements take no snapshot:
   ``SET LOCAL`` and ``LOCK TABLE`` of the three operator request outboxes ``IN SHARE MODE``
   (the web API inserts requests under no account lock; SHARE blocks every insert until the
   seed ends). The snapshot is taken by the next read, after the lock is granted: the
   runtime-session check again, then the realm stamp equals the manifest's, the latest
   ``capital_authority_epoch`` is ``legacy``, and no ledger row exists for any scope
   (``ledger_not_empty``). So no ledger or legacy write (writer locks) and no operator request
   (table lock) can land between the checks and the snapshot unseen.

Then, per scope: read the legacy closure (``execution.ledger_seed``), plan the rows and state
the expected per-table digests (``ledger.seed.write_seed``), write. After every scope is
written the transaction is switched to READ ONLY and each scope's digests are recomputed from
the database (``ledger.seed.verify_seed``); any difference rolls everything back (exit 2).
A refusal exits 3 with its reason code. Output is JSONL evidence, one object per line:
watermarks (legacy final event_seq, final snapshot event_seq/query id, trading_state max id),
the failed uncertainty request ids and the carried request ids per table, the ``recent_fill``
exposure (F7: per symbol the live credits still ``recent_fill`` and their share of total
capital, ``available + offered + credits``), expected and actual digests, and a summary. The
session locks are released after commit or rollback.

``--switch`` (the authority switch, ``bfx_ledger_switch.py`` on the VM) adds two steps to the
same transaction, after the writes and before the READ ONLY verification: the capture-point
closure verifier (``capital_comparison_closure.verify_closure``, every configured cell of
``--cells``) on the seed's own session -- any violation or incomplete coverage rolls back
(exit 4) -- and then the ``ledger`` epoch row (actor ``ledger_seed:<run id>``; evidence: run
id, the seed's observation/basis ids and expected digests, the closure summary, the F7
exposure), whose digest the verification includes. So a refusal, a closure violation or a
digest difference leaves neither ledger rows nor an epoch row.

``--check`` is the switch's read-only preview while legacy still runs: the manifest and owner
checks, then one REPEATABLE READ READ ONLY transaction without locks or the runtime-session
check (legacy is connected), in which the snapshot guards (realm, epoch, empty ledger) and per
scope the closure reader and the plan run. Every refusal is reported (the snapshot guards
together, and the first refusal of each scope's closure: the reader stops at its first), with
the F7 exposure of each seedable scope. Nothing is written; exit 0 means seedable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Final, Never, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

from bfx_funding_bot.apps.capital_comparison_closure import (
    CARRIED_TABLES,
    SeedEvidence,
    closure_json,
    summarize_closure,
    verify_closure,
)
from bfx_funding_bot.apps.capital_comparison_guard import GuardRejectedError, read_dsn
from bfx_funding_bot.apps.config import load_cells_only
from bfx_funding_bot.core.account_identity import account_id_canonical
from bfx_funding_bot.core.database_realm import (
    KNOWN_REALMS,
    DatabaseRealmMismatch,
    assert_database_realm,
)
from bfx_funding_bot.core.writer_lock import derive_lock_key, derive_transaction_lock_key
from bfx_funding_bot.modules.execution.ledger_seed import (
    fail_pending_uncertainty_requests,
    read_pending_uncertainty_requests,
    read_seed_closure,
)
from bfx_funding_bot.modules.ledger import Scope, SeedClosure, SeedRefused
from bfx_funding_bot.modules.ledger.seed import (
    EPOCH_TABLE,
    SeedResult,
    append_switch_epoch,
    epoch_rows,
    ledger_rows_in_scope,
    plan_seed,
    switch_epoch_row,
    verify_seed,
    with_epoch,
    write_seed,
)
from bfx_funding_bot.modules.ledger.table_digest import TableDigest
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_managed_offers,
    build_ledger_uncertainties,
)
from bfx_funding_bot.modules.trading import CapitalScope

RUNTIME_ROLES: Final = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
# Logins whose sessions must be gone: the runtime writers (and their members).
RUNTIME_WRITERS: Final = ("bfx_bot", "bfx_webapi")
OWNED_TABLES: Final = ("ledger_observation", "submission_attempt_journal", "capital_authority_epoch")
# The operator request outboxes (web API inserts, no account lock): held off during the seed.
REQUEST_TABLES: Final = (
    "uncertainty_resolution_requests", "capital_policy_requests", "trading_control_requests",
)
EXIT_OK, EXIT_DIGEST, EXIT_REFUSED, EXIT_CLOSURE = 0, 2, 3, 4
SWITCH_ACTOR_PREFIX: Final = "ledger_seed:"
SWITCH_REASON: Final = "authority switch after the legacy closure seed"
_SHARE_QUANTUM: Final = Decimal("0.000001")


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's error text includes untrusted arguments.
        raise GuardRejectedError("invalid_arguments")


def parser() -> argparse.ArgumentParser:
    result = SafeParser(description="legacy -> ledger closure seed (owner, halt only)",
                        allow_abbrev=False)
    result.add_argument("--dsn-file", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--authorize-seed", action="store_true")
    result.add_argument("--run-id", required=True)
    result.add_argument("--scope", action="append", required=True)
    result.add_argument("--cells", type=Path)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--switch", action="store_true")
    return result


@dataclass(frozen=True, slots=True)
class SeedPlanConnection:
    url: str
    user: str
    realm: str
    run_id: str
    scopes: tuple[Scope, ...]


def _scope(item: object) -> Scope:
    if isinstance(item, str):
        account, environment = item.split(":")
    elif isinstance(item, dict) and set(item) == {"account_id", "environment"}:
        account, environment = str(item["account_id"]), str(item["environment"])
    else:
        raise ValueError
    if environment not in KNOWN_REALMS:
        raise ValueError
    return Scope(UUID(account_id_canonical(account)), environment)


def validate_seed_connection(
    *, dsn: str, manifest_path: Path, run_id: str, scopes: Sequence[str], authorize_seed: bool,
    read_only: bool = False,
) -> SeedPlanConnection:
    """The manifest must name this DSN, this run and exactly these scopes.

    ``--authorize-seed`` is required for every mode that writes (not for ``--check``).
    """
    if not authorize_seed and not read_only:
        raise GuardRejectedError("seed_authorization_required")
    try:
        manifest = json.loads(manifest_path.read_text())
        url = make_url(dsn)
        if not isinstance(manifest, dict) or manifest.get("mode") != "seed":
            raise ValueError
        if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError
        if url.query or not url.host or any(c in url.host for c in ",/\\% \t\n"):
            raise ValueError
        if not url.username or not url.database or not url.port:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id) or manifest.get("run_id") != run_id:
            raise ValueError
        if type(manifest.get("port")) is not int:
            raise ValueError
        if url.host != manifest.get("host"):
            raise GuardRejectedError("host_mismatch")
        for actual, key in ((url.port, "port"), (url.database, "database"), (url.username, "user")):
            if actual != manifest.get(key):
                raise ValueError
        if manifest.get("realm") not in KNOWN_REALMS:
            raise ValueError
        listed = tuple(_scope(item) for item in manifest.get("scopes") or ())
        given = tuple(_scope(item) for item in scopes)
        if not given or len(set(given)) != len(given) or set(given) != set(listed):
            raise GuardRejectedError("scope_mismatch")
        if any(scope.deployment_environment != manifest["realm"] for scope in given):
            raise GuardRejectedError("scope_mismatch")
    except GuardRejectedError:
        raise
    except Exception:
        raise GuardRejectedError("invalid_seed_manifest") from None
    return SeedPlanConnection(
        url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False),
        url.username, str(manifest["realm"]), run_id,
        tuple(sorted(given, key=lambda s: (str(s.exchange_account_id), s.deployment_environment))),
    )


type Executor = AsyncSession | AsyncConnection


async def verify_owner(conn: Executor, plan: SeedPlanConnection) -> None:
    users = (await conn.execute(text("SELECT session_user, current_user"))).one()
    if users[0] != plan.user or users[1] != plan.user or plan.user in RUNTIME_ROLES:
        raise GuardRejectedError("dsn_not_owner")
    owners = set((await conn.execute(
        text("SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
             "JOIN pg_namespace n ON n.oid = c.relnamespace "
             "WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"),
        {"tables": list(OWNED_TABLES)},
    )).scalars())
    if owners != {plan.user}:
        raise GuardRejectedError("dsn_not_owner")


def _lock_keys(scope: Scope) -> tuple[int, int]:
    account = account_id_canonical(str(scope.exchange_account_id))
    return (derive_lock_key(account, scope.deployment_environment),
            derive_transaction_lock_key(account, scope.deployment_environment))


async def quiesce(conn: AsyncConnection, plan: SeedPlanConnection) -> None:
    """Before the seed's snapshot exists (autocommit): writers locked out, none connected.

    Session-level advisory locks on both writer keys of every scope (the daemon's lifetime
    lock and the per-transaction lock every legacy and ledger write takes), held until the
    connection releases them after commit or rollback; then no runtime login may be connected.
    A runtime login connecting later cannot write ledger or legacy rows (writer locks); its
    operator requests are held off by ``lock_requests`` inside the transaction.
    """
    for scope in plan.scopes:
        for key in _lock_keys(scope):
            taken = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            if not taken:
                raise GuardRejectedError("writer_lock_held")
    await refuse_runtime_sessions(conn)


async def lock_requests(session: AsyncSession) -> None:
    """The seed transaction's first lock: no operator request is inserted until it ends.

    ``LOCK TABLE`` takes no snapshot, so the REPEATABLE READ snapshot (taken by the next read)
    sees every request committed before the lock was granted, and none can follow it.
    """
    await session.execute(text("SET LOCAL lock_timeout = '30s'"))
    await session.execute(text(
        f"LOCK TABLE {', '.join('public.' + name for name in REQUEST_TABLES)} IN SHARE MODE"
    ))


async def refuse_runtime_sessions(conn: Executor) -> None:
    # Logins that are a runtime writer, or (recursively) a member of one.
    runtime = await conn.scalar(text("""
        WITH RECURSIVE writers(oid) AS (
          SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)
          UNION
          SELECT m.member FROM pg_auth_members m JOIN writers w ON m.roleid = w.oid)
        SELECT count(*) FROM pg_stat_activity a
        WHERE a.pid <> pg_backend_pid() AND a.usesysid IN (SELECT oid FROM writers)
    """), {"roles": list(RUNTIME_WRITERS)})
    if runtime:
        raise GuardRejectedError("runtime_session_present")


async def snapshot_refusals(session: AsyncSession, plan: SeedPlanConnection) -> list[str]:
    """Inside the REPEATABLE READ snapshot: isolation, realm, epoch, empty ledger, every one
    that fails, in that order (reads only)."""
    reasons: list[str] = []
    isolation = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
    if isolation != "repeatable read":
        reasons.append("seed_snapshot_isolation")
    try:
        await assert_database_realm(session, plan.realm)
    except DatabaseRealmMismatch:
        reasons.append("realm_mismatch")
    authority = await session.scalar(text(
        "SELECT authority FROM public.capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1"
    ))
    if authority != "legacy":
        reasons.append("epoch_not_legacy")
    for scope in plan.scopes:
        if await ledger_rows_in_scope(session, scope):
            reasons.append("ledger_not_empty")
            break
    return reasons


async def verify_snapshot(session: AsyncSession, plan: SeedPlanConnection) -> None:
    """The seed's snapshot guards; refuses with the first that fails."""
    reasons = await snapshot_refusals(session, plan)
    if reasons:
        raise GuardRejectedError(reasons[0])


def _digest_json(digest: TableDigest) -> dict[str, object]:
    return {"count": digest.count, "sha256": digest.sha256, "watermark": digest.watermark}


def emit(output: TextIO, value: object) -> None:
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n")
    output.flush()


def _scope_json(scope: Scope) -> dict[str, str]:
    return {"account_id": str(scope.exchange_account_id),
            "environment": scope.deployment_environment}


def recent_fill_exposure(closure: SeedClosure) -> dict[str, object]:
    """F7: per symbol, the live credits whose legacy group is still ``recent_fill`` (the
    ledger keeps them multi-cell until they end) and their share of total capital
    (``available + offered + credits``, as the capital policy reads it); ``max_share`` over
    the symbols. Amounts and shares are decimal strings (``share`` None for zero capital)."""
    symbols: dict[str, dict[str, object]] = {}
    shares: list[Decimal] = []
    totals = {s.symbol: s.available + s.offered + s.credits for s in closure.symbols}
    for symbol in sorted(totals.keys() | {g.symbol for g in closure.credit_groups}):
        recent = sum((g.amount for g in closure.credit_groups
                      if g.symbol == symbol and g.attribution_basis == "recent_fill"), Decimal(0))
        total = totals.get(symbol, Decimal(0))
        share = (recent / total).quantize(_SHARE_QUANTUM) if total > 0 else None
        if share is not None:
            shares.append(share)
        symbols[symbol] = {
            "recent_fill": format(recent, "f"), "total_capital": format(total, "f"),
            "share": None if share is None else format(share, "f"),
        }
    return {"symbols": symbols, "max_share": format(max(shares), "f") if shares else None}


def _seed_json(
    closure: SeedClosure, result: SeedResult, pending: Sequence[UUID], failed_requests: int,
) -> dict[str, object]:
    marks = closure.watermarks
    return {
        "kind": "seed",
        "scope": _scope_json(closure.scope),
        "watermarks": {
            "legacy_final_event_seq": marks.final_event_seq,
            "snapshot_event_seq": marks.snapshot_event_seq,
            "snapshot_query_id": str(marks.snapshot_query_id),
            "snapshot_command_fence": marks.snapshot_command_fence,
            "trading_state_max_id": marks.trading_state_max_id,
        },
        "observation_id": str(result.observation_id),
        "basis_id": str(result.basis_id),
        "attempt_seq_high_water": result.attempt_seq_high_water,
        "attempts": len(closure.attempts),
        "quarantines": len(closure.quarantines),
        "failed_requests": failed_requests,
        "failed_uncertainty_requests": [str(r) for r in pending],
        "carried_requests": closure.evidence.get("carried_pending_requests"),
        "recent_fill": recent_fill_exposure(closure),
        "expected": {name: _digest_json(d) for name, d in sorted(result.expected.items())},
    }


def connect(plan: SeedPlanConnection) -> AsyncEngine:
    return create_async_engine(plan.url, echo=False, hide_parameters=True,
                               connect_args={"timeout": 10})


@dataclass(frozen=True, slots=True)
class Seeded:
    closure: SeedClosure
    result: SeedResult
    pending: tuple[UUID, ...]


def load_cells(path: Path) -> tuple[tuple[str, str], ...]:
    """(symbol, cell id) of every configured cell; an unreadable or empty file refuses."""
    try:
        cells = tuple(sorted({(cell.symbol, cell.cell_id) for cell in load_cells_only(path)}))
    except Exception:
        raise GuardRejectedError("cells_unreadable") from None
    if not cells:
        raise GuardRejectedError("cells_unreadable")
    return cells


def seed_evidence(item: Seeded) -> SeedEvidence:
    """The closure verifier's view of one scope's seed, as its evidence line states it."""
    marks, scope = item.closure.watermarks, item.closure.scope
    carried = item.closure.evidence.get("carried_pending_requests")
    if not isinstance(carried, Mapping) or set(carried) != set(CARRIED_TABLES):
        raise GuardRejectedError("seed_evidence_invalid")
    return SeedEvidence(
        scope.exchange_account_id, scope.deployment_environment, item.result.observation_id,
        item.result.basis_id, marks.final_event_seq, marks.snapshot_event_seq,
        marks.snapshot_query_id, marks.trading_state_max_id, frozenset(item.pending),
        {name: frozenset(UUID(str(r)) for r in carried[name]) for name in CARRIED_TABLES},
    )


async def verify_capture_point(
    session: AsyncSession, seeded: Sequence[Seeded], cells: Sequence[tuple[str, str]],
    output: TextIO,
) -> dict[str, object]:
    """The capture-point closure verifier on the seed's own (uncommitted) writes."""
    scopes = [
        CapitalScope(item.closure.scope.exchange_account_id,
                     item.closure.scope.deployment_environment, symbol, cell_id)
        for item in seeded for symbol, cell_id in cells
    ]
    checks = await verify_closure(
        session, scopes=scopes,
        seeds={(e.account_id, e.environment): e for e in map(seed_evidence, seeded)},
        managed_offers=build_ledger_managed_offers(), uncertainties=build_ledger_uncertainties(),
    )
    for check in checks:
        emit(output, closure_json(check))
    summary = summarize_closure(scopes, checks)
    emit(output, {"kind": "closure_summary", **summary})
    return summary


async def append_epoch(
    session: AsyncSession, plan: SeedPlanConnection, seeded: Sequence[Seeded],
    closure: Mapping[str, object], *, now_ms: int, output: TextIO,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Append the ``ledger`` epoch in the seed transaction; returns (prior rows, new row)."""
    prior = await epoch_rows(session)
    evidence: dict[str, object] = {
        "run_id": plan.run_id,
        "seeds": [{
            "scope": _scope_json(item.closure.scope),
            "observation_id": str(item.result.observation_id),
            "basis_id": str(item.result.basis_id),
            "digests": {name: d.sha256 for name, d in sorted(item.result.expected.items())
                        if name != EPOCH_TABLE},
            "recent_fill": recent_fill_exposure(item.closure),
        } for item in seeded],
        "closure": {key: closure[key] for key in ("checks", "violations", "passed")},
    }
    row = switch_epoch_row(prior, now_ms=now_ms, actor=SWITCH_ACTOR_PREFIX + plan.run_id,
                           reason=SWITCH_REASON, evidence=evidence)
    await append_switch_epoch(session, row)
    emit(output, {"kind": "epoch", **row})
    return prior, row


async def seed(
    session: AsyncSession, plan: SeedPlanConnection, *, now_ms: int, output: TextIO,
    cells: Sequence[tuple[str, str]] | None = None,
) -> int:
    """Snapshot guards, closure, write and verification inside the caller's REPEATABLE READ
    transaction (``quiesce`` ran before it). With ``cells`` (``--switch``), the capture-point
    closure verifier and the epoch append run between the writes and the verification.
    Returns the exit code; the caller commits only on ``EXIT_OK``.
    """
    await session.execute(text("SET LOCAL search_path TO public"))
    await lock_requests(session)
    await refuse_runtime_sessions(session)  # the first snapshot read, after the lock
    await verify_snapshot(session, plan)
    seeded: list[Seeded] = []
    for scope in plan.scopes:
        closure = await read_seed_closure(session, scope)
        pending = await read_pending_uncertainty_requests(session, scope)
        result = await write_seed(session, closure)
        failed = await fail_pending_uncertainty_requests(session, scope, pending, now_ms=now_ms)
        seeded.append(Seeded(closure, result, pending))
        emit(output, _seed_json(closure, result, pending, failed))
    written = [item.result for item in seeded]
    if cells is not None:
        summary = await verify_capture_point(session, seeded, cells, output)
        if summary.get("passed") is not True:
            return EXIT_CLOSURE
        prior, row = await append_epoch(session, plan, seeded, summary, now_ms=now_ms,
                                        output=output)
        written = [replace(result, expected=with_epoch(result.expected, prior, row))
                   for result in written]
    # Keep the snapshot (and the writes), then read it back the way a verifier would.
    await session.execute(text("SET TRANSACTION READ ONLY"))
    exit_code = EXIT_OK
    for result in written:
        mismatches = await verify_seed(session, result.scope, result.expected)
        emit(output, {
            "kind": "verification",
            "scope": _scope_json(result.scope),
            "mismatches": [
                {"table": m.table, "expected": _digest_json(m.expected),
                 "actual": _digest_json(m.actual)}
                for m in mismatches
            ],
        })
        if mismatches:
            exit_code = EXIT_DIGEST
    return exit_code


async def _seed_transaction(
    conn: AsyncConnection, plan: SeedPlanConnection, *, now_ms: int, output: TextIO,
    cells: Sequence[tuple[str, str]] | None = None,
) -> int:
    """The one REPEATABLE READ transaction; commits only on ``EXIT_OK``."""
    await conn.commit()  # end SQLAlchemy's autobegun autocommit block (nothing to commit)
    await conn.execution_options(isolation_level="REPEATABLE READ")
    async with AsyncSession(bind=conn, autoflush=False, expire_on_commit=False,
                            join_transaction_mode="rollback_only") as session:
        transaction = await conn.begin()
        try:
            exit_code = await seed(session, plan, now_ms=now_ms, output=output, cells=cells)
        except BaseException:
            await transaction.rollback()
            raise
        if exit_code == EXIT_OK:
            await transaction.commit()
        else:
            await transaction.rollback()
    return exit_code


async def check(
    session: AsyncSession, plan: SeedPlanConnection, *, output: TextIO,
) -> list[dict[str, object]]:
    """``--check`` in the caller's READ ONLY snapshot: every refusal, nothing written."""
    refusals: list[dict[str, object]] = [
        {"scope": None, "reason": reason, "detail": []}
        for reason in await snapshot_refusals(session, plan)
    ]
    for scope in plan.scopes:
        line: dict[str, object] = {"kind": "check", "scope": _scope_json(scope)}
        try:
            closure = await read_seed_closure(session, scope)
            await read_pending_uncertainty_requests(session, scope)
            plan_seed(closure)
        except SeedRefused as exc:
            refusal: dict[str, object] = {"scope": _scope_json(scope), "reason": exc.reason,
                                          "detail": list(exc.detail)}
            refusals.append(refusal)
            line["refusal"] = refusal
        else:
            line["refusal"] = None
            line["attempts"] = len(closure.attempts)
            line["recent_fill"] = recent_fill_exposure(closure)
        emit(output, line)
    return refusals


async def _check_transaction(
    conn: AsyncConnection, plan: SeedPlanConnection, *, output: TextIO,
) -> list[dict[str, object]]:
    """One REPEATABLE READ READ ONLY transaction, always rolled back; no lock is taken."""
    await conn.commit()  # end SQLAlchemy's autobegun autocommit block (nothing to commit)
    await conn.execution_options(isolation_level="REPEATABLE READ")
    async with AsyncSession(bind=conn, autoflush=False, expire_on_commit=False,
                            join_transaction_mode="rollback_only") as session:
        transaction = await conn.begin()
        try:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL search_path TO public"))
            return await check(session, plan, output=output)
        finally:
            await transaction.rollback()


async def run(
    argv: Sequence[str], *, output: TextIO,
    connector: Callable[[SeedPlanConnection], AsyncEngine] = connect,
    clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> int:
    try:
        args = parser().parse_args(argv)
        mode = "check" if args.check else "switch" if args.switch else "seed"
        if mode == "switch" and args.cells is None:
            raise GuardRejectedError("cells_required")
        plan = validate_seed_connection(
            dsn=read_dsn(args.dsn_file), manifest_path=args.manifest, run_id=args.run_id,
            scopes=args.scope, authorize_seed=args.authorize_seed, read_only=mode == "check",
        )
        cells = load_cells(args.cells) if args.cells is not None else None
        engine = connector(plan)
        refusals: list[dict[str, object]] = []
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                try:
                    await verify_owner(conn, plan)
                    if mode == "check":
                        refusals = await _check_transaction(conn, plan, output=output)
                        exit_code = EXIT_REFUSED if refusals else EXIT_OK
                    else:
                        await quiesce(conn, plan)
                        exit_code = await _seed_transaction(
                            conn, plan, now_ms=clock(), output=output,
                            cells=cells if mode == "switch" else None,
                        )
                finally:
                    if conn.in_transaction():
                        await conn.rollback()
                    if mode != "check":
                        await conn.execution_options(isolation_level="AUTOCOMMIT")
                        await conn.execute(text("SELECT pg_advisory_unlock_all()"))
        finally:
            await engine.dispose()
        summary: dict[str, object] = {
            "kind": "summary", "mode": mode, "exit_code": exit_code, "run_id": plan.run_id,
            "committed": mode != "check" and exit_code == EXIT_OK,
        }
        if mode == "check":
            summary["refusals"] = refusals
        emit(output, summary)
        return exit_code
    except (GuardRejectedError, SeedRefused) as exc:
        with suppress(Exception):
            emit(output, {"kind": "summary", "exit_code": EXIT_REFUSED, "reason": exc.reason,
                          "detail": list(exc.detail), "committed": False})
        return EXIT_REFUSED
    except Exception:
        # Never serialize exceptions: driver errors may contain credentials.
        with suppress(Exception):
            emit(output, {"kind": "summary", "exit_code": EXIT_REFUSED,
                          "reason": "operational_failure", "committed": False})
        return EXIT_REFUSED


def main() -> int:
    return asyncio.run(run(sys.argv[1:], output=sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
