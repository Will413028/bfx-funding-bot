"""The one-time legacy -> ledger closure seed (S1-4d). Dormant: an owner tool for the halt.

Never referenced by deploy, compose or systemd (``tests/architecture`` pins it). It refuses to
write unless every guard holds, all inside the one REPEATABLE READ transaction it writes in:

* ``--authorize-seed`` and a manifest (mode ``seed``, run id, host, port, database, user,
  realm, scopes) that names exactly this DSN and these ``--scope`` arguments;
* the DSN's login is the ledger tables' owner, used directly (no SET ROLE), and is none of
  the runtime roles (the schema also refuses seed rows from any other role);
* the database realm stamp equals the manifest's;
* the latest ``capital_authority_epoch`` is ``legacy`` (the switch comes after the seed);
* no session of a runtime login (``bfx_bot``, ``bfx_webapi`` or a member of them) exists;
* per scope, the daemon writer lock and the transaction writer lock are free and taken here;
* per scope, no ledger row exists yet (``ledger_not_empty``).

Then, per scope: read the legacy closure (``execution.ledger_seed``), plan the rows and state
the expected per-table digests (``ledger.seed.write_seed``), write. After every scope is
written the transaction is switched to READ ONLY and each scope's digests are recomputed from
the database (``ledger.seed.verify_seed``); any difference rolls everything back (exit 2).
A refusal exits 3 with its reason code. Output is JSONL evidence, one object per line:
watermarks (legacy final event_seq, final snapshot event_seq/query id, trading_state max id),
expected and actual digests, and a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bfx_funding_bot.apps.capital_comparison_guard import GuardRejectedError, read_dsn
from bfx_funding_bot.core.account_identity import account_id_canonical
from bfx_funding_bot.core.database_realm import (
    KNOWN_REALMS,
    DatabaseRealmMismatch,
    assert_database_realm,
)
from bfx_funding_bot.core.writer_lock import derive_lock_key, derive_transaction_lock_key
from bfx_funding_bot.modules.execution.ledger_seed import read_seed_closure
from bfx_funding_bot.modules.ledger import Scope, SeedClosure, SeedRefused
from bfx_funding_bot.modules.ledger.seed import (
    SeedResult,
    ledger_rows_in_scope,
    verify_seed,
    write_seed,
)
from bfx_funding_bot.modules.ledger.table_digest import TableDigest

RUNTIME_ROLES: Final = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
# Logins whose sessions must be gone: the runtime writers (and their members).
RUNTIME_WRITERS: Final = ("bfx_bot", "bfx_webapi")
OWNED_TABLES: Final = ("ledger_observation", "submission_attempt_journal", "capital_authority_epoch")
EXIT_OK, EXIT_DIGEST, EXIT_REFUSED = 0, 2, 3


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
) -> SeedPlanConnection:
    """The manifest must name this DSN, this run and exactly these scopes."""
    if not authorize_seed:
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


async def verify_owner(session: AsyncSession, plan: SeedPlanConnection) -> None:
    users = (await session.execute(text("SELECT session_user, current_user"))).one()
    if users[0] != plan.user or users[1] != plan.user or plan.user in RUNTIME_ROLES:
        raise GuardRejectedError("dsn_not_owner")
    owners = set((await session.execute(
        text("SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
             "JOIN pg_namespace n ON n.oid = c.relnamespace "
             "WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"),
        {"tables": list(OWNED_TABLES)},
    )).scalars())
    if owners != {plan.user}:
        raise GuardRejectedError("dsn_not_owner")


async def verify_halt(session: AsyncSession, plan: SeedPlanConnection) -> None:
    try:
        await assert_database_realm(session, plan.realm)
    except DatabaseRealmMismatch:
        raise GuardRejectedError("realm_mismatch") from None
    authority = await session.scalar(text(
        "SELECT authority FROM public.capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1"
    ))
    if authority != "legacy":
        raise GuardRejectedError("epoch_not_legacy")
    # Logins that are a runtime writer, or (recursively) a member of one.
    runtime = await session.scalar(text("""
        WITH RECURSIVE writers(oid) AS (
          SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)
          UNION
          SELECT m.member FROM pg_auth_members m JOIN writers w ON m.roleid = w.oid)
        SELECT count(*) FROM pg_stat_activity a
        WHERE a.pid <> pg_backend_pid() AND a.usesysid IN (SELECT oid FROM writers)
    """), {"roles": list(RUNTIME_WRITERS)})
    if runtime:
        raise GuardRejectedError("runtime_session_present")
    for scope in plan.scopes:
        account = account_id_canonical(str(scope.exchange_account_id))
        for key in (derive_lock_key(account, scope.deployment_environment),
                    derive_transaction_lock_key(account, scope.deployment_environment)):
            taken = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key})
            if not taken:
                raise GuardRejectedError("writer_lock_held")
        if await ledger_rows_in_scope(session, scope):
            raise GuardRejectedError("ledger_not_empty")


def _digest_json(digest: TableDigest) -> dict[str, object]:
    return {"count": digest.count, "sha256": digest.sha256, "watermark": digest.watermark}


def emit(output: TextIO, value: object) -> None:
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n")
    output.flush()


def _scope_json(scope: Scope) -> dict[str, str]:
    return {"account_id": str(scope.exchange_account_id),
            "environment": scope.deployment_environment}


def _seed_json(closure: SeedClosure, result: SeedResult) -> dict[str, object]:
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
        "failed_requests": result.failed_requests,
        "expected": {name: _digest_json(d) for name, d in sorted(result.expected.items())},
    }


def connect(plan: SeedPlanConnection) -> AsyncEngine:
    return create_async_engine(plan.url, echo=False, hide_parameters=True,
                               connect_args={"timeout": 10})


async def seed(
    session: AsyncSession, plan: SeedPlanConnection, *, now_ms: int, output: TextIO,
) -> int:
    """Guards, closure, write and verification inside the caller's open transaction.

    Returns the exit code; the caller commits only on ``EXIT_OK``.
    """
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    await session.execute(text("SET LOCAL search_path TO public"))
    await verify_owner(session, plan)
    await verify_halt(session, plan)
    written: list[SeedResult] = []
    for scope in plan.scopes:
        closure = await read_seed_closure(session, scope)
        result = await write_seed(session, closure, now_ms=now_ms)
        written.append(result)
        emit(output, _seed_json(closure, result))
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


async def run(
    argv: Sequence[str], *, output: TextIO,
    connector: Callable[[SeedPlanConnection], AsyncEngine] = connect,
    clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> int:
    try:
        args = parser().parse_args(argv)
        plan = validate_seed_connection(
            dsn=read_dsn(args.dsn_file), manifest_path=args.manifest, run_id=args.run_id,
            scopes=args.scope, authorize_seed=args.authorize_seed,
        )
        engine = connector(plan)
        try:
            async with AsyncSession(engine, autoflush=False, expire_on_commit=False) as session:
                transaction = await session.begin()
                try:
                    exit_code = await seed(session, plan, now_ms=clock(), output=output)
                except BaseException:
                    await transaction.rollback()
                    raise
                if exit_code == EXIT_OK:
                    await transaction.commit()
                else:
                    await transaction.rollback()
        finally:
            await engine.dispose()
        emit(output, {"kind": "summary", "exit_code": exit_code, "run_id": plan.run_id,
                      "committed": exit_code == EXIT_OK})
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
