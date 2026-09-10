"""Version-one manifest envelope and length-delimited archive hashing.

This module is deliberately independent of SQLAlchemy and operator scripts.
Table entries use database column names (including ``period``), not ORM aliases.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import replace
from typing import Any
from uuid import UUID

from .codec import FORMAT_VERSION, decode_row, encode_row
from .contracts import ArchiveManifest, Scope, StreamIdentity

TABLE_NAMES = (
    "execution_uncertainties",
    "offer_claims",
    "position_state",
    "projection_heads",
    "reconcile_observation",
    "submission_attempts",
    "venue_credit_state",
    "venue_offer_state",
)


def digest_rows(rows: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.hexdigest()


def manifest_fields(manifest: ArchiveManifest) -> dict[str, object]:
    return {
        "run_id": manifest.run_id,
        "scope": {
            "account_id": manifest.scope.account_id,
            "environment": manifest.scope.environment,
        },
        "stream": {
            "count": manifest.stream.count,
            "head": manifest.stream.head,
            "digest": manifest.stream.digest,
        },
        "format_version": manifest.format_version,
        "image_digest": manifest.image_digest,
        "migration_heads": list(manifest.migration_heads),
        "projector_version": manifest.projector_version,
        "tables": list(manifest.tables),
    }


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _validate_shape(manifest: ArchiveManifest) -> None:
    if (
        not isinstance(manifest.run_id, UUID)
        or not isinstance(manifest.scope.account_id, UUID)
        or manifest.scope.environment not in {"prod", "shadow", "ci"}
        or type(manifest.format_version) is not int
        or manifest.format_version != FORMAT_VERSION
        or not isinstance(manifest.image_digest, str)
        or not manifest.image_digest
        or not isinstance(manifest.projector_version, str)
        or not manifest.projector_version
        or not manifest.migration_heads
        or any(not isinstance(h, str) or not h for h in manifest.migration_heads)
        or tuple(sorted(set(manifest.migration_heads))) != manifest.migration_heads
        or type(manifest.stream.count) is not int
        or manifest.stream.count < 0
        or type(manifest.stream.head) is not int
        or manifest.stream.head < manifest.stream.count
        or (manifest.stream.count == 0 and manifest.stream.head != 0)
        or not _is_digest(manifest.stream.digest)
    ):
        raise ValueError("invalid archive manifest identity")
    if tuple(t.get("name") for t in manifest.tables) != TABLE_NAMES:
        raise ValueError("archive table allowlist/order mismatch")
    for entry in manifest.tables:
        if set(entry) != {"name", "schema", "key_columns", "count", "digest"}:
            raise ValueError("invalid archive table fields")
        schema, keys = entry["schema"], entry["key_columns"]
        if not isinstance(schema, list) or not schema or not isinstance(keys, list) or not keys:
            raise ValueError("missing archive schema/key")
        names = []
        for column in schema:
            if (
                not isinstance(column, dict)
                or not isinstance(column.get("name"), str)
                or not column["name"]
                or not isinstance(column.get("type"), str)
                or type(column.get("nullable")) is not bool
            ):
                raise ValueError("invalid archive column schema")
            names.append(column["name"])
        if (
            len(set(names)) != len(names)
            or any(not isinstance(k, str) for k in keys)
            or len(set(keys)) != len(keys)
            or not set(keys) <= set(names)
        ):
            raise ValueError("invalid archive key columns")
        if type(entry["count"]) is not int or entry["count"] < 0 or not _is_digest(entry["digest"]):
            raise ValueError("invalid archive count/digest")


def seal_manifest(manifest: ArchiveManifest) -> ArchiveManifest:
    _validate_shape(manifest)
    return replace(
        manifest, digest=hashlib.sha256(encode_row(manifest_fields(manifest))).hexdigest()
    )


def validate_manifest(manifest: ArchiveManifest) -> None:
    if seal_manifest(manifest).digest != manifest.digest:
        raise ValueError("archive manifest digest mismatch")


def encode_manifest(manifest: ArchiveManifest) -> bytes:
    validate_manifest(manifest)
    return encode_row({**manifest_fields(manifest), "digest": manifest.digest})


def decode_manifest(payload: bytes) -> ArchiveManifest:
    try:
        fields: dict[str, Any] = decode_row(payload)
        result = ArchiveManifest(
            **{
                k: v
                for k, v in fields.items()
                if k not in {"scope", "stream", "tables", "migration_heads"}
            },
            scope=Scope(**fields["scope"]),
            stream=StreamIdentity(**fields["stream"]),
            tables=tuple(fields["tables"]),
            migration_heads=tuple(fields["migration_heads"]),
        )
        validate_manifest(result)
        return result
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid archive manifest envelope") from exc
