"""SELECT-only archive preservation verifier; expected manifests are independent."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_record
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.projection_cutover.archive import (
    _stream_identity,
    verify_archive,
)
from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row, encode_row
from bfx_funding_bot.modules.execution.projection_cutover.contracts import ArchiveManifest, Scope
from bfx_funding_bot.modules.execution.projection_cutover.manifest import (
    decode_manifest,
    validate_manifest,
)
from bfx_funding_bot.modules.execution.projection_cutover.tables import ArchiveRun

MAX_INPUT_BYTES = 1024 * 1024
MAX_RUNS = 32


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


def decode_archive_inputs(raw: bytes) -> tuple[ArchiveManifest, ...]:
    """Decode an opaque, digest-pinned transport using the original Task2 codec."""
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("archive input oversized")
    try:
        envelope = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(envelope, dict) or set(envelope) != {"schema_version", "prepared"}
                or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
                or not isinstance(envelope["prepared"], list) or len(envelope["prepared"]) > MAX_RUNS):
            raise ValueError("archive input version/shape")
        manifests = []
        for item in envelope["prepared"]:
            if not isinstance(item, dict) or set(item) != {"sha256", "payload"}:
                raise ValueError("archive prepared shape")
            payload = base64.b64decode(item["payload"], validate=True)
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ValueError("archive prepared digest")
            prepared = decode_row(payload)
            if (set(prepared) != {"kind", "diagnostic_digest", "classification_digest",
                                  "operational_digest", "runtime_roles", "snapshot", "manifest"}
                    or prepared["kind"] != "projection-cutover-prepared-v1"):
                raise ValueError("archive prepared kind/shape")
            manifest_fields = prepared["manifest"]
            if not isinstance(manifest_fields, dict):
                raise ValueError("archive prepared manifest shape")
            manifests.append(decode_manifest(encode_row(manifest_fields)))
        return tuple(manifests)
    except (TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError("archive input invalid") from exc


async def verify_archives(
    session: AsyncSession, *, scope: Scope, expected: tuple[ArchiveManifest, ...],
) -> list[dict[str, Any]]:
    """Require exact completed inventory even for explicit legacy empty input."""
    if len(expected) > MAX_RUNS or len({m.run_id for m in expected}) != len(expected):
        raise ValueError("archive inventory invalid")
    for manifest in expected:
        validate_manifest(manifest)
        if manifest.scope != scope:
            raise ValueError("archive scope mismatch")
    with session.no_autoflush:
        schema_exists = await session.scalar(text(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname='projection_audit')"
        ))
        if not schema_exists:
            heads = set(await session.scalars(text("SELECT version_num FROM public.alembic_version")))
            if expected or "f8c2d4e6a901" in heads:
                raise ValueError("archive inventory absent")
            return []
        inventory = set(await session.scalars(
            select(ArchiveRun.run_id).where(
                ArchiveRun.exchange_account_id == scope.account_id,
                ArchiveRun.deployment_environment == scope.environment,
                ArchiveRun.complete.is_(True),
            ).limit(MAX_RUNS + 1)
        ))
        if inventory != {m.run_id for m in expected}:
            raise ValueError("archive inventory mismatch")
        reports = []
        for manifest in sorted(expected, key=lambda m: str(m.run_id)):
            await verify_archive(session, expected=manifest)
            reports.append({
                "scope": {"account_id": str(scope.account_id), "environment": scope.environment},
                "run_id": str(manifest.run_id), "manifest_digest": manifest.digest,
                "verified_tables": [t["name"] for t in manifest.tables],
                "verified_counts": {t["name"]: t["count"] for t in manifest.tables},
                "verified_digests": {t["name"]: t["digest"] for t in manifest.tables},
            })
        return reports


async def _verify_original_prefix(session: AsyncSession, manifest: ArchiveManifest) -> None:
    hasher = hashlib.sha256(b"[")
    count = head = 0
    result = await session.stream_scalars(select(EventLogRow).from_statement(text(
        "SELECT * FROM public.event_log WHERE exchange_account_id=:account "
        "AND deployment_environment=:environment AND event_seq<=:head ORDER BY event_seq"
    )).params(account=manifest.scope.account_id, environment=manifest.scope.environment,
              head=manifest.stream.head).execution_options(yield_per=256))
    try:
        async for row in result:
            if count:
                hasher.update(b",")
            hasher.update(json.dumps(canonical_event_record(row), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True, default=str).encode())
            count += 1
            head = row.event_seq
    finally:
        await result.close()
    hasher.update(b"]")
    if (count, head, hasher.hexdigest()) != (manifest.stream.count, manifest.stream.head, manifest.stream.digest):
        raise ValueError("archive original event identity mismatch")


async def verify_restore_archives(
    session: AsyncSession, *, scope: Scope, raw: bytes, archive_only: bool,
) -> dict[str, Any]:
    expected = decode_archive_inputs(raw)
    if archive_only and not expected:
        raise ValueError("archive-only requires independent manifests")
    reports = await verify_archives(session, scope=scope, expected=expected)
    with session.no_autoflush:
        stream = await _stream_identity(session, scope)
    heads = list(await session.scalars(text("SELECT version_num FROM public.alembic_version ORDER BY version_num")))
    pins = {m.run_id: item["sha256"] for m, item in zip(expected, json.loads(raw)["prepared"], strict=True)}
    manifests = {str(m.run_id): m for m in expected}
    for report in reports:
        manifest = manifests[report["run_id"]]
        with session.no_autoflush:
            await _verify_original_prefix(session, manifest)
        if archive_only and (manifest.stream != stream or list(manifest.migration_heads) != heads):
            raise ValueError("archive-only original event/schema identity mismatch")
        report.update({
            "original_event_count": manifest.stream.count, "original_event_head": manifest.stream.head,
            "original_event_hash": manifest.stream.digest, "image_digest": manifest.image_digest,
            "projector_version": manifest.projector_version, "migration_heads": list(manifest.migration_heads),
            "prepared_digest": pins[manifest.run_id],
        })
    report = {"schema_version": 1, "scope": {"account_id": str(scope.account_id), "environment": scope.environment},
              "event_count": stream.count, "event_head": stream.head if stream.count else None,
              "event_hash": stream.digest, "migration_heads": heads, "archives": reports,
              "archive_only": archive_only}
    if len(json.dumps(report).encode()) > 65536:
        raise ValueError("archive report oversized")
    return report


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    with os.fdopen(os.open(args.input, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        meta = os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > MAX_INPUT_BYTES:
            raise ValueError("archive input file invalid")
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES or hashlib.sha256(raw).hexdigest() != args.input_digest:
        raise ValueError("archive input pin mismatch")
    engine = create_async_engine(os.environ["DATABASE_URL"], isolation_level="REPEATABLE READ")
    try:
        async with async_sessionmaker(engine)() as session:
            return await verify_restore_archives(session, scope=Scope(UUID(args.account_id), args.environment),
                                                 raw=raw, archive_only=args.archive_only)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", choices=("prod", "shadow", "ci"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-digest", required=True)
    parser.add_argument("--archive-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(_run(args))
    except Exception:
        print('{"error":"archive_verification_failed"}')
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
