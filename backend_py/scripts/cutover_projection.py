"""Explicit archive-only operator commands. This module never starts a daemon."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Never
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.crypto import load_kek
from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.accounts.vault import load_account_credentials
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    SnapshotCoverage,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.projection_cutover.archive import (
    _describe,
    _stream_identity,
    capture_archive,
    verify_archive,
)
from bfx_funding_bot.modules.execution.projection_cutover.codec import (
    decode_row,
    encode_row,
    row_digest,
)
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
from bfx_funding_bot.modules.execution.projection_cutover.diagnostics import compare_rows
from bfx_funding_bot.modules.execution.projection_cutover.manifest import (
    TABLE_NAMES,
    decode_manifest,
    digest_rows,
    encode_manifest,
)
from bfx_funding_bot.modules.execution.projection_cutover.snapshot import validate_cutover_snapshot
from bfx_funding_bot.modules.execution.protocols import AccountContext
from scripts.projection_cutover_operations import (
    Runner,
    run_fixed,
    verify_database_quiescence,
    verify_local_operations,
)
from scripts.verify_projection_replay import _archive_projection_rows, replay_one_account


async def prepare_archive(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, run_id: UUID,
    image_digest: str, projector_version: str, diagnostic: dict[str, Any],
    classification_payload: bytes, classification_digest: str,
    snapshot: VenueSnapshotObserved, managed_symbols: frozenset[str], now_ms: int,
    max_age_ms: int, operation_inventory: dict[str, Any], runtime_roles: tuple[str, ...],
    output: Path, dry_run: bool, prepared_digest: str | None = None, runner: Runner = run_fixed,
) -> dict[str, Any]:
    started = time.monotonic()
    identities = {"run_id": run_id, "scope": asdict(scope), "image_digest": image_digest,
                  "projector_version": projector_version}
    if (diagnostic.get("kind") != "projection-cutover-diagnostic-v1"
            or any(diagnostic.get(key) != value for key, value in identities.items())
            or sorted(table["name"] for table in diagnostic["tables"]) != list(TABLE_NAMES)
            or operation_inventory["images"]["bot"] != image_digest):
        raise ValueError("prepare_identity_mismatch")
    validate_classification(diagnostic, classification_payload, expected_digest=classification_digest)
    validate_cutover_snapshot(snapshot, scope=scope, managed_symbols=managed_symbols,
                              now_ms=now_ms, max_age_ms=max_age_ms)
    binding = {
        "kind": "projection-cutover-prepared-v1", "diagnostic_digest": row_digest(diagnostic),
        "classification_digest": classification_digest, "snapshot": serialize_event(snapshot),
        "operational_digest": row_digest(operation_inventory), "runtime_roles": list(runtime_roles),
    }
    expected: dict[str, Any] | None = None
    if prepared_digest is not None:
        expected = decode_row(read_private(output, expected_digest=prepared_digest))
        if set(expected) != {*binding, "manifest"} or any(expected[key] != value for key, value in binding.items()):
            raise ValueError("prepare_repeat_mismatch")
    elif output.exists() or output.is_symlink():
        raise ValueError("prepare_evidence_exists")
    await verify_local_operations(operation_inventory, runner=runner)
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        await session.execute(text("SET LOCAL lock_timeout='2s'"))
        await session.execute(text("SET LOCAL statement_timeout='30s'"))
        await AccountEventWriter(store=PostgresEventStore(deployment_environment=scope.environment)).acquire_lock(
            session, account_id=scope.account_id,
        )
        # SHARE locks fence direct SQL writers too; event/halt cannot drift while
        # capture validates projections. No rows are changed by these locks.
        for name in ("event_log", "trading_halt", *TABLE_NAMES):
            await session.execute(text(f"LOCK TABLE public.{name} IN SHARE MODE"))
        await verify_database_quiescence(session, scope=scope, runtime_roles=runtime_roles)
        validate_cutover_snapshot(snapshot, scope=scope, managed_symbols=managed_symbols,
            now_ms=now_ms + int((time.monotonic() - started) * 1000), max_age_ms=max_age_ms)
        await verify_local_operations(operation_inventory, runner=runner)
        if asdict(await _stream_identity(session, scope)) != diagnostic["stream"]:
            raise ValueError("prepare_stream_drift")
        rows = await _archive_projection_rows(session, account_id=scope.account_id, environment=scope.environment)
        for table in diagnostic["tables"]:
            connection = await session.connection()
            _, schema, keys = await connection.run_sync(partial(_describe, name=table["name"]))
            if {**_table_facts(table["name"], rows[table["name"]], keys), "schema": schema} != table:
                raise ValueError("prepare_original_projection_drift")
        if expected is not None:
            manifest = decode_manifest(encode_row(expected["manifest"]))
            if any(getattr(manifest, key) != value for key, value in {
                "scope": scope, "run_id": run_id, "image_digest": image_digest,
                "projector_version": projector_version,
            }.items()):
                raise ValueError("prepare_repeat_manifest_mismatch")
            await verify_archive(session, expected=manifest)
            result = expected
        else:
            manifest = await capture_archive(session, scope=scope, run_id=run_id,
                                             image_digest=image_digest, projector_version=projector_version)
            for table, original in zip(manifest.tables, diagnostic["tables"], strict=True):
                if any(table[key] != value for key, value in original.items()):
                    raise ValueError("prepare_archive_source_mismatch")
            result = {**binding, "manifest": decode_row(encode_manifest(manifest))}
        await verify_database_quiescence(session, scope=scope, runtime_roles=runtime_roles)
        await verify_local_operations(operation_inventory, runner=runner)
        validate_cutover_snapshot(snapshot, scope=scope, managed_symbols=managed_symbols,
            now_ms=now_ms + int((time.monotonic() - started) * 1000), max_age_ms=max_age_ms)
        if dry_run or expected is not None:
            await session.rollback()
        else:
            # Save independent evidence before commit. If commit fails this file
            # remains an uncommitted attempt, and repeat verification rejects it.
            write_private(output, encode_row(result))
            await session.commit()
        return result


def _table_facts(
    name: str, rows: Sequence[Mapping[str, object]], keys: Sequence[str],
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: encode_row({key: row[key] for key in keys}))
    return {"name": name, "key_columns": list(keys), "count": len(rows),
            "digest": digest_rows(encode_row(row) for row in ordered)}


async def diagnose(
    factory: async_sessionmaker[AsyncSession], *, scope: Scope, run_id: UUID,
    image_digest: str, projector_version: str,
) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []

    def collect(
        table: str, before: Sequence[Mapping[str, object]], after: Sequence[Mapping[str, object]],
        *, key_columns: tuple[str, ...],
    ) -> None:
        tables.append(_table_facts(table, before, key_columns))
        differences.extend(asdict(difference) for difference in compare_rows(
            table, before, after, key_columns=key_columns,
        ))

    # The optional collector starts its own fresh REPEATABLE READ transaction.
    # Closing this session rolls it back; prepare uses a new READ COMMITTED one.
    async with factory() as session:
        report = await replay_one_account(
            session, account_id=scope.account_id, environment=scope.environment,
            projector_version=projector_version, diagnostic_collector=collect,
        )
        connection = await session.connection()
        for table in tables:
            _, schema, keys = await connection.run_sync(partial(_describe, name=table["name"]))
            if keys != table["key_columns"]:
                raise ValueError("diagnostic_key_schema_mismatch")
            table["schema"] = schema
    return {
        "kind": "projection-cutover-diagnostic-v1", "run_id": run_id,
        "scope": asdict(scope), "image_digest": image_digest, "projector_version": projector_version,
        "stream": {"count": report.row_counts["event_log"], "head": report.event_head or 0,
                   "digest": report.event_hash},
        "tables": sorted(tables, key=lambda table: table["name"]), "differences": differences,
    }


def validate_classification(diagnostic: dict[str, Any], payload: bytes, *, expected_digest: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ValueError("classification_artifact_drift")
    review = decode_row(payload)
    if set(review) != {"diagnostic_digest", "reviewer", "classifications"} or (
        review["diagnostic_digest"] != row_digest(diagnostic)
        or not isinstance(review["reviewer"], str) or not review["reviewer"].strip()
    ):
        raise ValueError("classification_review_missing")
    entries = review["classifications"]
    if not isinstance(entries, list):
        raise ValueError("classification_invalid")
    expected = [encode_row(difference) for difference in diagnostic["differences"]]
    actual = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "table", "key_digest", "column", "before_digest", "after_digest", "classification",
            "reason", "evidence",
        } or entry["classification"] not in {
            "identity_representation", "historical_state", "time_sequence", "missing_symbol",
            "checkpoint_source",
        } or any(not isinstance(entry[key], str) or not entry[key].strip()
                 for key in ("reason", "evidence")):
            raise ValueError("classification_unexplained")
        actual.append(encode_row({
            **{key: value for key, value in entry.items() if key not in {"reason", "evidence"}},
            "classification": "unexplained",
        }))
    if len(actual) != len(set(actual)) or sorted(actual) != sorted(expected):
        raise ValueError("classification_difference_mismatch")


def write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def read_private(path: Path, *, expected_digest: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > 64 * 1024 * 1024):
            raise ValueError("evidence_file_not_private")
        payload = source.read()
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ValueError("evidence_digest_mismatch")
    return payload


async def collect_snapshot(
    *, scope: Scope, managed_symbols: frozenset[str], ctx: AccountContext,
    max_age_ms: int, transport: httpx.AsyncBaseTransport | None = None,
) -> VenueSnapshotObserved:
    if ctx.account_id != str(scope.account_id):
        raise ValueError("snapshot_credential_scope_mismatch")
    wire: dict[str, list[Any]] = {}

    async def inspect_response(response: httpx.Response) -> None:
        await response.aread()
        if response.status_code != 200:
            raise ValueError("venue_read_failed")
        value = json.loads(response.content, parse_float=Decimal)
        if not isinstance(value, list):
            raise ValueError("venue_response_incomplete")
        wire[response.request.url.path.rsplit("/", 1)[-1]] = value

    start = int(time.time() * 1000)
    try:
        async with httpx.AsyncClient(
            transport=transport, event_hooks={"response": [inspect_response]},
        ) as http:
            client = BitfinexAuthREST(http=http)
            offers = await client.get_active_funding_offers(ctx=ctx)
            credits = await client.get_active_funding_credits(ctx=ctx)
            wallets = await client.get_funding_available_all(ctx=ctx)
        # The legacy wallet parser conservatively treats unavailable balances
        # as zero. Such a value cannot establish complete cutover evidence.
        explicit_wallets: dict[str, Decimal] = {}
        for row in wire["wallets"]:
            if row[0] == "funding":
                symbol = row[1] if row[1].startswith("f") else "f" + row[1]
                if row[4] is None or symbol in explicit_wallets:
                    raise ValueError("wallet_evidence_unknown")
                explicit_wallets[symbol] = Decimal(str(row[4]))
        if explicit_wallets != wallets:
            raise ValueError("wallet_representation_loss")
        # Preserve the existing normalized representation; refuse numeric loss
        # in a parser rather than silently trusting its rounded amount.
        if any(credit.amount != abs(Decimal(str(row[5])))
               for credit, row in zip(credits, wire["credits"], strict=True)):
            raise ValueError("credit_representation_loss")
        snapshot = VenueSnapshotObserved(
            account_id=ctx.account_id, environment=scope.environment,
            query_started_at_ms=start, query_finished_at_ms=int(time.time() * 1000),
            offers=tuple(VenueOfferObservation(
                venue_offer_id=o.venue_offer_id, symbol=o.symbol,
                amount_original=o.amount_original if o.amount_original is not None else o.amount,
                amount_remaining=o.amount,
                rate=o.rate_decimal if o.rate_observed else None,
                period_days=o.period_days, status=o.status, mts_created=o.mts_created,
                mts_updated=o.mts_updated if o.mts_updated is not None else o.mts_created,
                offer_type=o.offer_type,
                flags=o.flags if isinstance(o.flags, dict) else {} if o.flags is None else {"raw": o.flags},
            ) for o in offers),
            credits=tuple(VenueCreditObservation(
                credit_id=c.credit_id, symbol=c.symbol, amount=c.amount,
                rate=Decimal(str(row[9])) if row[9] is not None else None,
                period_days=c.period_days, status=c.status,
                mts_created=c.mts_created, mts_updated=c.mts_updated,
                flags=c.flags if isinstance(c.flags, dict) else {} if c.flags is None else {"raw": c.flags},
            ) for c, row in zip(credits, wire["credits"], strict=True)),
            wallet_available=wallets, coverage=SnapshotCoverage(True, True, True),
        )
        validate_cutover_snapshot(snapshot, scope=scope, managed_symbols=managed_symbols,
                                  now_ms=int(time.time() * 1000), max_age_ms=max_age_ms)
        return snapshot
    except Exception:
        # Venue/shape exceptions may embed raw response data or request secrets.
        raise ValueError("snapshot_collection_blocked") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("invalid_arguments")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _Parser()
    parser.add_argument("command", choices=("diagnose", "prepare", "verify-archive", "apply"))
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--environment", required=True, choices=("ci", "shadow", "prod"))
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--projector-version", required=True, choices=("execution-state-v1",))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--diagnostic", type=Path)
    parser.add_argument("--diagnostic-digest")
    parser.add_argument("--classification", type=Path)
    parser.add_argument("--classification-digest")
    parser.add_argument("--operations", type=Path)
    parser.add_argument("--operations-digest")
    parser.add_argument("--prepared-digest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_digest):
        raise ValueError("image_identity_invalid")
    if args.dry_run and args.command != "prepare":
        raise ValueError("invalid_dry_run_command")
    return args


async def run_command(
    args: argparse.Namespace, *, runner: Runner = run_fixed,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if args.command == "apply":
        raise ValueError("apply_not_implemented")
    scope = Scope(args.account_id, args.environment)
    identity = {"scope": scope, "run_id": args.run_id, "image_digest": args.image_digest,
                "projector_version": args.projector_version}
    # Only explicit process environment. Never discover/load a production .env.
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        if args.command == "diagnose":
            if args.output.exists() or args.output.is_symlink():
                raise ValueError("diagnostic_target_exists")
            diagnostic = await diagnose(factory, **identity)
            payload = encode_row(diagnostic)
            write_private(args.output, payload)
            return {"status": "diagnosed", "diagnostic_digest": hashlib.sha256(payload).hexdigest(),
                    "difference_count": len(diagnostic["differences"])}
        if args.command == "verify-archive":
            prepared: dict[str, Any] = decode_row(read_private(args.output, expected_digest=args.prepared_digest))
            if prepared.get("kind") != "projection-cutover-prepared-v1":
                raise ValueError("prepared_format_invalid")
            manifest = decode_manifest(encode_row(prepared["manifest"]))
            if any(getattr(manifest, key) != value for key, value in identity.items()):
                raise ValueError("verification_identity_mismatch")
            async with factory() as session:
                await verify_archive(session, expected=manifest)
            return {"status": "verified", "manifest_digest": manifest.digest}
        if not all((args.diagnostic, args.diagnostic_digest, args.classification,
                    args.classification_digest, args.operations, args.operations_digest)):
            raise ValueError("prepare_evidence_required")
        diagnostic = decode_row(read_private(args.diagnostic, expected_digest=args.diagnostic_digest))
        classification = read_private(args.classification, expected_digest=args.classification_digest)
        inventory = decode_row(read_private(args.operations, expected_digest=args.operations_digest))
        roles = inventory.get("runtime_roles")
        if not isinstance(roles, dict) or set(roles) != {"bot", "webapi", "frontend", "weekly-report"} or any(
            not isinstance(role, str) or not role.strip() for role in roles.values()
        ):
            raise ValueError("runtime_inventory_incomplete")
        max_age_ms = int(os.environ.get("BFX_HALT2_MAX_SNAPSHOT_AGE_SECONDS", "300")) * 1000
        symbols = frozenset({"fUST", "fUSD"})
        await verify_local_operations(inventory, runner=runner)
        if args.prepared_digest:
            repeat_evidence: dict[str, Any] = decode_row(read_private(args.output, expected_digest=args.prepared_digest))
            snapshot = deserialize_event("VENUE_SNAPSHOT_OBSERVED", repeat_evidence["snapshot"])
            if not isinstance(snapshot, VenueSnapshotObserved):
                raise ValueError("prepared_snapshot_invalid")
        else:
            async with factory() as session:
                credentials = await load_account_credentials(session, exchange_account_id=scope.account_id, kek=load_kek())
            # Venue IO runs after closing the credential session and before the
            # capture transaction; BootRecovery.run is never invoked.
            snapshot = await collect_snapshot(
                scope=scope, managed_symbols=symbols, max_age_ms=max_age_ms,
                ctx=AccountContext(account_id=str(scope.account_id), credentials=credentials,
                                   allocation_cap_usdt=Decimal("0")), transport=transport,
            )
        result = await prepare_archive(
            factory, **identity, diagnostic=diagnostic, classification_payload=classification,
            classification_digest=args.classification_digest, snapshot=snapshot, managed_symbols=symbols,
            now_ms=int(time.time() * 1000), max_age_ms=max_age_ms, operation_inventory=inventory,
            runtime_roles=tuple(sorted(set(roles.values()))), output=args.output, dry_run=args.dry_run,
            prepared_digest=args.prepared_digest, runner=runner,
        )
        return {"status": "dry_run" if args.dry_run else "prepared",
                "manifest_digest": result["manifest"]["digest"],
                "prepared_digest": row_digest(result)}
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = asyncio.run(run_command(parse_args(argv)))
    except Exception:
        print(json.dumps({"error": "cutover_blocked"}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
