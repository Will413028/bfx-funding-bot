"""Atomic cutover of one halted scope; the caller owns commit and rollback.

Operator artifact I/O is completed before this boundary. Only bounded verified
headers and a digest-pinned restore receipt cross into runtime. No venue reads,
operator imports, archive restoration, or transaction commits belong here.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from typing import Any, Protocol, cast
from uuid import uuid5

from sqlalchemy import JSON, Table, Text, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _projection_evidence,
    _projection_values,
    replay_captured_rows,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_stored_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.event_store.writer import (
    DEFAULT_PROJECTOR_VERSION,
    AccountEventWriter,
)
from bfx_funding_bot.modules.execution.events import VenueSnapshotObserved

from .archive import (
    _describe,
    _source_spool,
    audit_runtime_roles,
    capture_archive,
    verify_archive,
)
from .codec import decode_row, encode_row, row_digest
from .contracts import ArchiveManifest, Scope, StreamIdentity
from .evidence import (
    CLASSIFICATION_KIND,
    DIAGNOSTIC_KIND,
    VerifiedCutoverEvidence,
    _manifest_digest,
)
from .manifest import TABLE_NAMES, decode_manifest, digest_rows, encode_manifest, validate_manifest
from .snapshot import validate_cutover_snapshot
from .tables import ArchiveReceipt

_SYMBOLS = frozenset({"fUST", "fUSD"})
_CLASSIFICATIONS = frozenset({
    "identity_representation", "historical_state", "time_sequence", "missing_symbol", "checkpoint_source",
})


class QuiescenceVerifier(Protocol):
    async def __call__(
        self, session: AsyncSession, *, scope: Scope,
        runtime_roles: tuple[str, ...], operation_digest: str,
    ) -> None: ...


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_apply_evidence(expected: ArchiveManifest, evidence: VerifiedCutoverEvidence) -> None:
    """Recheck compact header seals and the cross-artifact identity binding."""
    validate_manifest(expected)
    if not isinstance(evidence, VerifiedCutoverEvidence):
        raise ValueError("evidence_format_invalid")
    for manifest, kind in ((evidence.diagnostic, DIAGNOSTIC_KIND),
                           (evidence.classification, CLASSIFICATION_KIND)):
        if (manifest.kind != kind or manifest.format_version != 2
                or _manifest_digest(manifest) != manifest.digest
                or any(getattr(manifest, field) != getattr(expected, field) for field in (
                    "run_id", "scope", "stream", "image_digest", "projector_version",
                ))):
            raise ValueError("apply_evidence_identity_mismatch")
    if (expected.projector_version != DEFAULT_PROJECTOR_VERSION
            or evidence.diagnostic.tables != expected.tables
            or evidence.classification.diagnostic_digest != evidence.diagnostic.digest
            or evidence.classification.record_count != evidence.diagnostic.record_count
            or len(dict(evidence.classification_counts)) != len(evidence.classification_counts)
            or any(name not in _CLASSIFICATIONS or type(count) is not int or count <= 0
                   for name, count in evidence.classification_counts)
            or sum(count for _, count in evidence.classification_counts) != evidence.diagnostic.record_count):
        raise ValueError("apply_evidence_binding_mismatch")


def validate_restore_receipt_preflight(expected: ArchiveManifest, receipt: Mapping[str, Any]) -> None:
    """Validate bounded receipt identity without authorizing a new apply.

    The CLI checks the independent receipt/transport/prepared byte pins. Here
    we recheck their bounded decoded identity, then bind it to the run receipt.
    Freshness is deferred until runtime rules out an identical committed retry.
    """
    if len(encode_row(receipt)) > 131072:
        raise ValueError("restore_receipt_oversized")
    stream, scope = expected.stream, expected.scope
    common = {"event_count": stream.count, "event_head": stream.head or None,
              "event_hash": stream.digest, "migration_heads": list(expected.migration_heads)}
    required = {**common, "schema_version": 2, "kind": "archive_restore", "measured": True,
        "target_run_id": str(expected.run_id), "account_id": str(scope.account_id),
        "environment": scope.environment, "projector_version": expected.projector_version,
        "verifier_image_digest": expected.image_digest, "network_internal": True,
        "egress_disconnected": True, "verifier_exit_status": 0}
    if any(receipt.get(k) != v or type(receipt.get(k)) is not type(v) for k, v in required.items()):
        raise ValueError("restore_receipt_identity_mismatch")
    if (any(not _digest(receipt.get(k)) for k in ("archive_input_digest", "config_digest"))
            or any(not isinstance(receipt.get(k), str) or not receipt[k].strip()
                   for k in ("restore_run_id", "target_backup_label", "network_name"))
            or any(type(receipt.get(k)) is not int or receipt[k] < 0
                   for k in ("observed_at_ms", "rto_seconds", "server_version_num"))
            or receipt["server_version_num"] // 10000 != 18
            or not isinstance(receipt.get("image_digest"), str)
            or not receipt["image_digest"].startswith("sha256:")
            or not _digest(receipt["image_digest"][7:])
            or not isinstance(receipt.get("image_labels"), dict)
            or (receipt.get("target_time") is not None and not isinstance(receipt["target_time"], str))):
        raise ValueError("restore_receipt_invalid")
    report = receipt.get("archive_verification")
    report_identity = {**common, "schema_version": 2, "archive_only": True,
        "target_run_id": str(expected.run_id),
        "scope": {"account_id": str(scope.account_id), "environment": scope.environment}}
    if not isinstance(report, dict) or any(
        report.get(k) != v or type(report.get(k)) is not type(v) for k, v in report_identity.items()
    ):
        raise ValueError("restore_archive_target_mismatch")
    archives = report.get("archives")
    if (not isinstance(archives, list) or not 1 <= len(archives) <= 32
            or any(not isinstance(a, dict) for a in archives)
            or len({a.get("run_id") for a in archives}) != len(archives)):
        raise ValueError("restore_archive_inventory_invalid")
    targets = [a for a in archives if a.get("run_id") == str(expected.run_id)]
    fields = {"scope": report_identity["scope"], "manifest_digest": expected.digest,
        "verified_tables": list(TABLE_NAMES),
        "verified_counts": {t["name"]: t["count"] for t in expected.tables},
        "verified_digests": {t["name"]: t["digest"] for t in expected.tables},
        "original_event_count": stream.count, "original_event_head": stream.head,
        "original_event_hash": stream.digest, "image_digest": expected.image_digest,
        "projector_version": expected.projector_version, "migration_heads": list(expected.migration_heads)}
    if (len(targets) != 1 or any(targets[0].get(k) != v for k, v in fields.items())
            or not _digest(targets[0].get("prepared_digest"))):
        raise ValueError("restore_archive_manifest_mismatch")


def validate_restore_receipt(
    expected: ArchiveManifest, receipt: Mapping[str, Any], *, now_ms: int,
) -> None:
    """Require matching archive evidence within the closed DR 900-second window."""
    validate_restore_receipt_preflight(expected, receipt)
    if type(now_ms) is not int or not 0 <= now_ms - receipt["observed_at_ms"] <= 900_000:
        raise ValueError("restore_receipt_time_invalid")


async def _verify_live_archive(session: AsyncSession, expected: ArchiveManifest) -> None:
    """Compare actual full-column bytes, including unmapped historical fields."""
    connection = await session.connection()
    for entry in expected.tables:
        table, schema, keys = await connection.run_sync(partial(_describe, name=str(entry["name"])))
        if schema != entry["schema"] or keys != entry["key_columns"]:
            raise ValueError("apply_source_schema_drift")
        json_names = {c.name for c in table.c if isinstance(c.type, JSON)}
        query = select(*[sql_cast(c, Text).label(c.name) if c.name in json_names else c for c in table.c]).where(
            table.c.exchange_account_id == expected.scope.account_id,
            table.c.deployment_environment == expected.scope.environment,
        )
        with _source_spool() as spool:
            result = await session.stream(query.execution_options(yield_per=256))
            count = 0
            try:
                async for row in result.mappings():
                    values = _projection_values(dict(row), json_names=json_names)
                    spool.execute("INSERT INTO source_rows VALUES (?, ?)", (
                        encode_row({key: values[key] for key in keys}), encode_row(values),
                    ))
                    count += 1
            finally:
                await result.close()
            digest = digest_rows(row[0] for row in spool.execute("SELECT payload FROM source_rows ORDER BY row_key"))
        if count != entry["count"] or digest != entry["digest"]:
            raise ValueError("apply_source_projection_drift")


async def _captured_rows(session: AsyncSession, scope: Scope) -> Sequence[EventLogRow]:
    return (await session.scalars(select(EventLogRow).from_statement(text(
        "SELECT * FROM public.event_log WHERE exchange_account_id=:id "
        "AND deployment_environment=:env ORDER BY event_seq"
    )).params(id=scope.account_id, env=scope.environment).execution_options(populate_existing=True))).all()


async def _current_stream(session: AsyncSession, scope: Scope) -> StreamIdentity:
    rows = await _captured_rows(session, scope)
    return StreamIdentity(len(rows), rows[-1].event_seq if rows else 0, canonical_event_hash(rows))


async def _verify_parity(
    session: AsyncSession, *, expected: ArchiveManifest,
    snapshot: VenueSnapshotObserved, rows: Sequence[EventLogRow], stream: StreamIdentity,
) -> dict[str, str]:
    scope = expected.scope
    replay = await replay_captured_rows(session, rows=rows, account_id=scope.account_id,
        environment=scope.environment, projector_version=expected.projector_version,
        expected_event_hash=stream.digest)
    counts, hashes, offers, credits = await _projection_evidence(
        session, account_id=scope.account_id, environment=scope.environment, streaming=True,
    )
    if (replay.event_head != stream.head
            or any(replay.row_counts.get(k) != v for k, v in counts.items())
            or any(replay.content_hashes.get(k) != v for k, v in hashes.items())
            or offers != replay.replayed_offer_exposure_by_symbol
            or credits != replay.replayed_credit_exposure_by_symbol):
        raise ValueError("apply_projection_parity_mismatch")
    wanted_offers = {s: sum((o.amount_remaining for o in snapshot.offers if o.symbol == s), Decimal(0)) for s in _SYMBOLS}
    wanted_credits = {s: sum((c.amount for c in snapshot.credits if c.symbol == s), Decimal(0)) for s in _SYMBOLS}
    positions = (await session.scalars(select(PositionStateRow).where(
        PositionStateRow.exchange_account_id == scope.account_id,
        PositionStateRow.deployment_environment == scope.environment,
    ).execution_options(populate_existing=True))).all()
    if (set(offers) - _SYMBOLS or set(credits) - _SYMBOLS
            or {p.symbol for p in positions} != _SYMBOLS
            or any(offers.get(s, Decimal(0)) != wanted_offers[s]
                   or credits.get(s, Decimal(0)) != wanted_credits[s] for s in _SYMBOLS)
            or any(p.available_amount != snapshot.wallet_available[p.symbol]
                   or p.offered_amount != wanted_offers[p.symbol]
                   or p.lent_amount != wanted_credits[p.symbol]
                   or p.uncertain_amount != 0 for p in positions)):
        raise ValueError("apply_exposure_coverage_mismatch")
    return hashes


async def _insert_receipt(session: AsyncSession, *, expected: ArchiveManifest, receipt: Mapping[str, Any]) -> None:
    await session.execute(cast(Table, ArchiveReceipt.__table__).insert().values(
        run_id=expected.run_id, snapshot_identity=encode_row(receipt["request"]),
        new_stream=encode_row(receipt["new_stream"]), active_hashes=encode_row(receipt["active_hashes"]),
        evidence=encode_row(receipt),
    ))


async def apply_cutover(
    session: AsyncSession, *, expected: ArchiveManifest, snapshot: VenueSnapshotObserved,
    archive_restore_receipt: Mapping[str, object], evidence: VerifiedCutoverEvidence,
    runtime_roles: tuple[str, ...], operation_digest: str,
    quiescence_verifier: QuiescenceVerifier, now_ms: int,
) -> Mapping[str, object]:
    started = time.monotonic()
    if not session.in_transaction():
        raise ValueError("apply requires active caller transaction")
    store = PostgresEventStore(deployment_environment=expected.scope.environment)
    # This must precede receipt lookup, freshness and any projection inspection.
    await AccountEventWriter(store=store).acquire_lock(session, account_id=expected.scope.account_id)
    connection = await session.connection()
    if await connection.get_isolation_level() != "READ COMMITTED":
        raise ValueError("apply requires READ COMMITTED")
    validate_apply_evidence(expected, evidence)
    validate_restore_receipt_preflight(expected, archive_restore_receipt)
    if (not _digest(operation_digest) or not runtime_roles
            or any(not isinstance(r, str) or not r.strip() for r in runtime_roles)
            or tuple(sorted(set(runtime_roles))) != runtime_roles):
        raise ValueError("apply_operation_identity_invalid")
    request = {"manifest_digest": expected.digest, "snapshot": serialize_event(snapshot),
        "diagnostic_digest": evidence.diagnostic.digest,
        "classification_digest": evidence.classification.digest,
        "restore_receipt_digest": row_digest(archive_restore_receipt),
        "operation_digest": operation_digest, "runtime_roles": list(runtime_roles)}
    receipt_table = cast(Table, ArchiveReceipt.__table__)
    saved = (await session.execute(select(receipt_table).where(receipt_table.c.run_id == expected.run_id))).mappings().one_or_none()
    completed: dict[str, Any] | None = decode_row(saved["evidence"]) if saved is not None else None
    if completed is not None and (completed["request"] != request
            or saved is None or saved["snapshot_identity"] != encode_row(request)
            or saved["new_stream"] != encode_row(completed["new_stream"])
            or saved["active_hashes"] != encode_row(completed["active_hashes"])):
        raise ValueError("apply_completed_request_mismatch")
    # Fence direct SQL writes too. SHARE ROW EXCLUSIVE permits isolated replay
    # reads and serializes upgrades; no self-blocking SHARE -> write conversion.
    for name in ("event_log", "trading_halt", *TABLE_NAMES):
        await session.execute(text(f"LOCK TABLE public.{name} IN SHARE ROW EXCLUSIVE MODE"))
    await audit_runtime_roles(session, role_names=runtime_roles)
    halted = await session.scalar(text(
        "SELECT halted FROM public.trading_halt WHERE exchange_account_id=:id "
        "AND deployment_environment=:env ORDER BY id DESC LIMIT 1"
    ), {"id": expected.scope.account_id, "env": expected.scope.environment})
    if halted is not True:
        raise ValueError("apply_scope_not_halted")
    await quiescence_verifier(session, scope=expected.scope, runtime_roles=runtime_roles,
                             operation_digest=operation_digest)
    await verify_archive(session, expected=expected)
    heads = tuple(await session.scalars(text("SELECT version_num FROM public.alembic_version ORDER BY version_num")))
    if heads != expected.migration_heads:
        raise ValueError("apply_migration_drift")
    if completed is not None:
        applied = decode_manifest(encode_row(completed["applied_manifest"]))
        if (applied.run_id != uuid5(expected.run_id, "projection-cutover-applied-v1")
                or applied.scope != expected.scope or applied.image_digest != expected.image_digest
                or applied.projector_version != expected.projector_version
                or asdict(applied.stream) != completed["new_stream"]
                or await _current_stream(session, expected.scope) != applied.stream):
            raise ValueError("apply_completed_stream_drift")
        await verify_archive(session, expected=applied)
        await _verify_live_archive(session, applied)
        return completed
    validate_cutover_snapshot(snapshot, scope=expected.scope, managed_symbols=_SYMBOLS,
                              now_ms=now_ms + int((time.monotonic() - started) * 1000), max_age_ms=300_000)
    if await _current_stream(session, expected.scope) != expected.stream:
        raise ValueError("apply_stream_drift")
    await _verify_live_archive(session, expected)
    # Include lock wait and all source checks, immediately before first mutation.
    validate_restore_receipt(expected, archive_restore_receipt,
                            now_ms=now_ms + int((time.monotonic() - started) * 1000))
    await store.append_snapshot(session, snapshot)
    await store.rebuild_snapshot_from_log(session, account_id=str(expected.scope.account_id),
                                          deployment_environment=expected.scope.environment)
    await session.flush()
    rows = await _captured_rows(session, expected.scope)
    prefix = [r for r in rows if r.event_seq <= expected.stream.head]
    if (len(prefix) != expected.stream.count or canonical_event_hash(prefix) != expected.stream.digest
            or len(rows) != expected.stream.count + 1 or rows[-1].event_id != snapshot.event_id
            or rows[-1].event_seq <= expected.stream.head
            or serialize_event(deserialize_stored_event(rows[-1])) != serialize_event(snapshot)):
        raise ValueError("apply_event_prefix_or_head_mismatch")
    stream = StreamIdentity(len(rows), rows[-1].event_seq, canonical_event_hash(rows))
    hashes = await _verify_parity(session, expected=expected, snapshot=snapshot, rows=rows, stream=stream)
    applied = await capture_archive(session, scope=expected.scope,
        run_id=uuid5(expected.run_id, "projection-cutover-applied-v1"),
        image_digest=expected.image_digest, projector_version=expected.projector_version)
    if applied.stream != stream:
        raise ValueError("apply_new_stream_drift")
    await verify_archive(session, expected=applied)
    await _verify_live_archive(session, applied)
    await quiescence_verifier(session, scope=expected.scope, runtime_roles=runtime_roles,
                             operation_digest=operation_digest)
    final_hashes = (await _projection_evidence(session, account_id=expected.scope.account_id,
        environment=expected.scope.environment, streaming=True))[1]
    if final_hashes != hashes or await _current_stream(session, expected.scope) != stream:
        raise ValueError("apply_final_parity_mismatch")
    validate_cutover_snapshot(snapshot, scope=expected.scope, managed_symbols=_SYMBOLS,
                              now_ms=now_ms + int((time.monotonic() - started) * 1000), max_age_ms=300_000)
    validate_restore_receipt(expected, archive_restore_receipt,
                            now_ms=now_ms + int((time.monotonic() - started) * 1000))
    receipt = {"kind": "projection-cutover-applied-v1", "run_id": expected.run_id,
        "snapshot_event_id": snapshot.event_id, "request": request, "new_stream": asdict(stream),
        "active_hashes": hashes, "applied_manifest": decode_row(encode_manifest(applied))}
    await _insert_receipt(session, expected=expected, receipt=receipt)
    return receipt
