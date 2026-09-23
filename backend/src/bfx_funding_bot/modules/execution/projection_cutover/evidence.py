"""Private, bounded v2 evidence artifacts for projection cutover.

The artifact wire format is intentionally small and boring: a canonical
``encode_row`` manifest and length-delimited records in private chunk files.
The reader verifies the directory, manifest, frames, and digests while it
streams records; callers must consume the iterator to run the final checks.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

from .codec import decode_row, encode_row
from .contracts import Scope, StreamIdentity

FORMAT_VERSION = 2
DIAGNOSTIC_KIND = "projection-cutover-diagnostic-v2"
CLASSIFICATION_KIND = "projection-cutover-classification-v2"
DIFFERENCE_RECORD_KIND = "difference"
CLASSIFICATION_RECORD_KIND = "classification"

MAX_RECORD_PAYLOAD_BYTES = 1 << 20
MAX_PART_BYTES = 4 << 20
MAX_ARTIFACT_BYTES = 2 << 30
MAX_PART_COUNT = 4096

# A manifest is metadata, not a record, but it must still have a bounded
# allocation before the untrusted codec payload is decoded. Four thousand
# descriptors fit comfortably below this ceiling.
MAX_MANIFEST_BYTES = 16 << 20

# Descriptive aliases make the fixed wire limits unambiguous to consumers.
MAX_RECORD_BYTES = MAX_RECORD_PAYLOAD_BYTES
MAX_CHUNK_BYTES = MAX_PART_BYTES
MAX_TOTAL_BYTES = MAX_ARTIFACT_BYTES
MAX_CHUNKS = MAX_PART_COUNT

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_FRAME_PREFIX_BYTES = 8
_ALLOWED_KINDS = {DIAGNOSTIC_KIND, CLASSIFICATION_KIND}
_RECORD_KIND_BY_KIND = {
    DIAGNOSTIC_KIND: DIFFERENCE_RECORD_KIND,
    CLASSIFICATION_KIND: CLASSIFICATION_RECORD_KIND,
}
_ROOT_ENTRIES = {"manifest", "COMPLETE", "chunks"}
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkDescriptor:
    name: str
    record_count: int
    byte_count: int
    digest: str


@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    kind: str
    format_version: int
    run_id: UUID
    scope: Scope
    image_digest: str
    projector_version: str
    stream: StreamIdentity
    record_kind: str
    record_count: int
    total_bytes: int
    chunks: tuple[ChunkDescriptor, ...]
    root_digest: str
    digest: str
    tables: tuple[Mapping[str, object], ...] = ()
    diagnostic_digest: str | None = None
    reviewer: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedCutoverEvidence:
    diagnostic: EvidenceManifest
    classification: EvidenceManifest
    classification_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _ManifestHeader:
    kind: str
    format_version: int
    run_id: UUID
    scope: Scope
    image_digest: str
    projector_version: str
    stream: StreamIdentity
    record_kind: str
    tables: tuple[Mapping[str, object], ...]
    diagnostic_digest: str | None
    reviewer: str | None


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_digest(value: object, *, label: str) -> str:
    if not _is_digest(value):
        raise ValueError(f"invalid evidence {label}")
    return cast(str, value)


def _validate_scope(scope: object) -> Scope:
    if not isinstance(scope, Scope):
        raise ValueError("invalid evidence scope")
    if (
        not isinstance(scope.account_id, UUID)
        or not isinstance(scope.environment, str)
        or scope.environment not in {
        "prod",
        "shadow",
        "ci",
        }
    ):
        raise ValueError("invalid evidence scope")
    return scope


def _validate_stream(stream: object) -> StreamIdentity:
    if not isinstance(stream, StreamIdentity):
        raise ValueError("invalid evidence stream")
    if (
        not _is_int(stream.count)
        or stream.count < 0
        or not _is_int(stream.head)
        or stream.head < 0
        or stream.head < stream.count
        or (stream.count == 0 and stream.head != 0)
        or not _is_digest(stream.digest)
    ):
        raise ValueError("invalid evidence stream")
    return stream


def _copy_tables(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("invalid evidence tables")
    tables: list[Mapping[str, object]] = []
    for table in value:
        if not isinstance(table, Mapping) or not all(isinstance(key, str) for key in table):
            raise ValueError("invalid evidence table facts")
        copied = dict(table)
        try:
            encode_row(copied)
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("invalid evidence table facts") from exc
        tables.append(copied)
    return tuple(tables)


def _normalize_header(manifest_fields: Mapping[str, object]) -> _ManifestHeader:
    if not isinstance(manifest_fields, Mapping):
        raise ValueError("evidence manifest fields must be a mapping")

    kind = manifest_fields.get("kind")
    if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported evidence kind")
    expected_fields = {
        "kind",
        "format_version",
        "run_id",
        "scope",
        "image_digest",
        "projector_version",
        "stream",
        "record_kind",
    }
    if kind == DIAGNOSTIC_KIND:
        expected_fields.add("tables")
    else:
        expected_fields.update({"diagnostic_digest", "reviewer"})
    if set(manifest_fields) != expected_fields:
        raise ValueError("invalid evidence manifest header fields")

    format_version = manifest_fields.get("format_version")
    run_id = manifest_fields.get("run_id")
    image_digest = manifest_fields.get("image_digest")
    projector_version = manifest_fields.get("projector_version")
    record_kind = manifest_fields.get("record_kind")
    if (
        not _is_int(format_version)
        or format_version != FORMAT_VERSION
        or not isinstance(run_id, UUID)
        or not isinstance(image_digest, str)
        or not image_digest
        or not isinstance(projector_version, str)
        or not projector_version
        or record_kind != _RECORD_KIND_BY_KIND[kind]
    ):
        raise ValueError("invalid evidence manifest header")

    scope = _validate_scope(manifest_fields.get("scope"))
    stream = _validate_stream(manifest_fields.get("stream"))
    tables = _copy_tables(manifest_fields.get("tables", ()))
    diagnostic_digest: str | None = None
    reviewer: str | None = None
    if kind == CLASSIFICATION_KIND:
        diagnostic_digest = _require_digest(
            manifest_fields.get("diagnostic_digest"), label="diagnostic digest"
        )
        reviewer_value = manifest_fields.get("reviewer")
        if not isinstance(reviewer_value, str) or not reviewer_value.strip():
            raise ValueError("invalid evidence reviewer")
        reviewer = reviewer_value

    return _ManifestHeader(
        kind=kind,
        format_version=format_version,
        run_id=run_id,
        scope=scope,
        image_digest=image_digest,
        projector_version=projector_version,
        stream=stream,
        record_kind=record_kind,
        tables=tables if kind == DIAGNOSTIC_KIND else (),
        diagnostic_digest=diagnostic_digest,
        reviewer=reviewer,
    )


def _chunk_fields(chunk: ChunkDescriptor) -> dict[str, object]:
    return {
        "name": chunk.name,
        "record_count": chunk.record_count,
        "byte_count": chunk.byte_count,
        "digest": chunk.digest,
    }


def _manifest_fields(manifest: EvidenceManifest) -> dict[str, object]:
    fields: dict[str, object] = {
        "kind": manifest.kind,
        "format_version": manifest.format_version,
        "run_id": manifest.run_id,
        "scope": {
            "account_id": manifest.scope.account_id,
            "environment": manifest.scope.environment,
        },
        "image_digest": manifest.image_digest,
        "projector_version": manifest.projector_version,
        "stream": {
            "count": manifest.stream.count,
            "head": manifest.stream.head,
            "digest": manifest.stream.digest,
        },
        "record_kind": manifest.record_kind,
        "record_count": manifest.record_count,
        "total_bytes": manifest.total_bytes,
        "chunks": [_chunk_fields(chunk) for chunk in manifest.chunks],
        "root_digest": manifest.root_digest,
    }
    if manifest.kind == DIAGNOSTIC_KIND:
        fields["tables"] = [dict(table) for table in manifest.tables]
    elif manifest.kind == CLASSIFICATION_KIND:
        fields["diagnostic_digest"] = manifest.diagnostic_digest
        fields["reviewer"] = manifest.reviewer
    return fields


def _validate_manifest_shape(manifest: EvidenceManifest) -> None:
    if manifest.kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported evidence kind")
    if (
        not _is_int(manifest.format_version)
        or manifest.format_version != FORMAT_VERSION
        or not isinstance(manifest.run_id, UUID)
        or not isinstance(manifest.image_digest, str)
        or not manifest.image_digest
        or not isinstance(manifest.projector_version, str)
        or not manifest.projector_version
        or manifest.record_kind != _RECORD_KIND_BY_KIND[manifest.kind]
    ):
        raise ValueError("invalid evidence manifest identity")
    _validate_scope(manifest.scope)
    _validate_stream(manifest.stream)
    if manifest.kind == DIAGNOSTIC_KIND:
        if manifest.diagnostic_digest is not None or manifest.reviewer is not None:
            raise ValueError("invalid diagnostic evidence fields")
        _copy_tables(manifest.tables)
    elif manifest.tables or not _is_digest(manifest.diagnostic_digest):
        raise ValueError("invalid classification evidence fields")
    if not isinstance(manifest.reviewer, (str, type(None))):
        raise ValueError("invalid evidence reviewer")
    if manifest.kind == CLASSIFICATION_KIND and (
        manifest.reviewer is None or not manifest.reviewer.strip()
    ):
        raise ValueError("invalid evidence reviewer")
    if (
        not _is_int(manifest.record_count)
        or manifest.record_count < 0
        or not _is_int(manifest.total_bytes)
        or manifest.total_bytes < 0
        or manifest.total_bytes > MAX_ARTIFACT_BYTES
        or len(manifest.chunks) > MAX_PART_COUNT
        or not _is_digest(manifest.root_digest)
        or not _is_digest(manifest.digest)
    ):
        raise ValueError("invalid evidence manifest bounds")

    expected_total = 0
    expected_count = 0
    for index, chunk in enumerate(manifest.chunks):
        if (
            not isinstance(chunk, ChunkDescriptor)
            or chunk.name != f"{index:08d}.part"
            or not _is_int(chunk.record_count)
            or chunk.record_count < 0
            or not _is_int(chunk.byte_count)
            or chunk.byte_count < 0
            or chunk.byte_count > MAX_PART_BYTES
            or not _is_digest(chunk.digest)
            or (chunk.record_count == 0 and chunk.byte_count != 0)
            or (chunk.record_count > 0 and chunk.byte_count < _FRAME_PREFIX_BYTES)
        ):
            raise ValueError("invalid evidence chunk descriptor")
        expected_total += chunk.byte_count
        expected_count += chunk.record_count
        if expected_total > MAX_ARTIFACT_BYTES:
            raise ValueError("evidence artifact exceeds size limit")
    if (
        expected_total != manifest.total_bytes
        or expected_count != manifest.record_count
        or (not manifest.chunks and (manifest.record_count != 0 or manifest.total_bytes != 0))
    ):
        raise ValueError("evidence manifest count or byte mismatch")


def _manifest_digest(manifest: EvidenceManifest) -> str:
    _validate_manifest_shape(manifest)
    return hashlib.sha256(encode_row(_manifest_fields(manifest))).hexdigest()


def _seal_manifest(manifest: EvidenceManifest) -> EvidenceManifest:
    _validate_manifest_shape(manifest)
    digest = hashlib.sha256(encode_row(_manifest_fields(manifest))).hexdigest()
    return EvidenceManifest(
        kind=manifest.kind,
        format_version=manifest.format_version,
        run_id=manifest.run_id,
        scope=manifest.scope,
        image_digest=manifest.image_digest,
        projector_version=manifest.projector_version,
        stream=manifest.stream,
        record_kind=manifest.record_kind,
        record_count=manifest.record_count,
        total_bytes=manifest.total_bytes,
        chunks=manifest.chunks,
        root_digest=manifest.root_digest,
        digest=digest,
        tables=manifest.tables,
        diagnostic_digest=manifest.diagnostic_digest,
        reviewer=manifest.reviewer,
    )


def _manifest_payload(manifest: EvidenceManifest) -> bytes:
    sealed = _seal_manifest(manifest)
    return encode_row({**_manifest_fields(sealed), "digest": sealed.digest})


def _mapping_field(fields: Mapping[str, object], name: str) -> object:
    try:
        return fields[name]
    except KeyError as exc:
        raise ValueError("invalid evidence manifest fields") from exc


def _parse_scope(value: object) -> Scope:
    if not isinstance(value, Mapping) or set(value) != {"account_id", "environment"}:
        raise ValueError("invalid evidence scope")
    scope = Scope(
        account_id=_mapping_field(value, "account_id"),  # type: ignore[arg-type]
        environment=_mapping_field(value, "environment"),  # type: ignore[arg-type]
    )
    return _validate_scope(scope)


def _parse_stream(value: object) -> StreamIdentity:
    if not isinstance(value, Mapping) or set(value) != {"count", "head", "digest"}:
        raise ValueError("invalid evidence stream")
    stream = StreamIdentity(
        count=_mapping_field(value, "count"),  # type: ignore[arg-type]
        head=_mapping_field(value, "head"),  # type: ignore[arg-type]
        digest=_mapping_field(value, "digest"),  # type: ignore[arg-type]
    )
    return _validate_stream(stream)


def _parse_chunks(value: object) -> tuple[ChunkDescriptor, ...]:
    if not isinstance(value, list):
        raise ValueError("invalid evidence chunks")
    chunks: list[ChunkDescriptor] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "name",
            "record_count",
            "byte_count",
            "digest",
        }:
            raise ValueError("invalid evidence chunk descriptor")
        chunks.append(
            ChunkDescriptor(
                name=_mapping_field(raw, "name"),  # type: ignore[arg-type]
                record_count=_mapping_field(raw, "record_count"),  # type: ignore[arg-type]
                byte_count=_mapping_field(raw, "byte_count"),  # type: ignore[arg-type]
                digest=_mapping_field(raw, "digest"),  # type: ignore[arg-type]
            )
        )
    return tuple(chunks)


def _parse_tables(value: object) -> tuple[Mapping[str, object], ...]:
    return _copy_tables(value)


def _decode_manifest_payload(payload: bytes, *, expected_kind: str) -> EvidenceManifest:
    if expected_kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported expected evidence kind")
    try:
        fields = decode_row(payload)
        expected_fields = {
            "kind",
            "format_version",
            "run_id",
            "scope",
            "image_digest",
            "projector_version",
            "stream",
            "record_kind",
            "record_count",
            "total_bytes",
            "chunks",
            "root_digest",
            "digest",
        }
        if expected_kind == DIAGNOSTIC_KIND:
            expected_fields.add("tables")
        else:
            expected_fields.update({"diagnostic_digest", "reviewer"})
        if fields.get("kind") != expected_kind:
            raise ValueError("evidence kind mismatch")
        if set(fields) != expected_fields:
            raise ValueError("invalid evidence manifest fields")

        tables = _parse_tables(fields["tables"]) if expected_kind == DIAGNOSTIC_KIND else ()
        diagnostic_digest = (
            _require_digest(fields["diagnostic_digest"], label="diagnostic digest")
            if expected_kind == CLASSIFICATION_KIND
            else None
        )
        reviewer = fields["reviewer"] if expected_kind == CLASSIFICATION_KIND else None
        if expected_kind == CLASSIFICATION_KIND and (
            not isinstance(reviewer, str) or not reviewer.strip()
        ):
            raise ValueError("invalid evidence reviewer")
        manifest = EvidenceManifest(
            kind=fields["kind"],  # type: ignore[arg-type]
            format_version=fields["format_version"],  # type: ignore[arg-type]
            run_id=fields["run_id"],  # type: ignore[arg-type]
            scope=_parse_scope(fields["scope"]),
            image_digest=fields["image_digest"],  # type: ignore[arg-type]
            projector_version=fields["projector_version"],  # type: ignore[arg-type]
            stream=_parse_stream(fields["stream"]),
            record_kind=fields["record_kind"],  # type: ignore[arg-type]
            record_count=fields["record_count"],  # type: ignore[arg-type]
            total_bytes=fields["total_bytes"],  # type: ignore[arg-type]
            chunks=_parse_chunks(fields["chunks"]),
            root_digest=fields["root_digest"],  # type: ignore[arg-type]
            digest=fields["digest"],  # type: ignore[arg-type]
            tables=tables,
            diagnostic_digest=diagnostic_digest,
            reviewer=reviewer,  # type: ignore[arg-type]
        )
        _validate_manifest_shape(manifest)
        if not hmac.compare_digest(_manifest_digest(manifest), manifest.digest):
            raise ValueError("evidence manifest digest mismatch")
        return manifest
    except (KeyError, TypeError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("invalid evidence manifest") from exc


def _no_follow_flags(*, directory: bool = False) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | os.O_NONBLOCK | no_follow
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _check_directory_info(info: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE
        or info.st_uid != os.getuid()
    ):
        raise ValueError("evidence directory is not private to current owner")


def _check_file_info(info: os.stat_result, *, max_bytes: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != _FILE_MODE
        or info.st_uid != os.getuid()
        or info.st_size < 0
        or info.st_size > max_bytes
    ):
        raise ValueError("evidence file is not private or bounded")


def _open_directory(path: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open(path, _no_follow_flags(directory=True))
        _check_directory_info(os.fstat(descriptor))
        return descriptor
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("invalid evidence directory") from exc


def _open_directory_at(parent: int, name: str) -> int:
    descriptor = -1
    try:
        descriptor = os.open(name, _no_follow_flags(directory=True), dir_fd=parent)
        _check_directory_info(os.fstat(descriptor))
        return descriptor
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("invalid evidence chunks directory") from exc


def _read_up_to(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        data = os.read(descriptor, min(64 * 1024, limit + 1 - total))
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if total > limit:
            raise ValueError("evidence file exceeds size limit")
    return b"".join(chunks)


def _read_regular_at(parent: int, name: str, *, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(name, _no_follow_flags(), dir_fd=parent)
        initial = os.fstat(descriptor)
        _check_file_info(initial, max_bytes=max_bytes)
        payload = _read_up_to(descriptor, max_bytes)
        final = os.fstat(descriptor)
        _check_file_info(final, max_bytes=max_bytes)
        if final.st_size != initial.st_size or final.st_size != len(payload):
            raise ValueError("evidence file changed while reading")
        return payload
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("could not read evidence file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_regular_at(parent: int, name: str, *, expected_bytes: int) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        descriptor = os.open(name, _no_follow_flags(), dir_fd=parent)
        info = os.fstat(descriptor)
        _check_file_info(info, max_bytes=MAX_PART_BYTES)
        if info.st_size != expected_bytes:
            raise ValueError("evidence chunk byte count mismatch")
        return descriptor, info
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("could not open evidence chunk") from exc


def _read_exact(descriptor: int, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        data = os.read(descriptor, size - len(buffer))
        if not data:
            raise ValueError("truncated evidence frame")
        buffer.extend(data)
    return bytes(buffer)


@contextmanager
def _opened_artifact(
    path: Path, *, expected_digest: str, expected_kind: str
) -> Iterator[tuple[int, int, EvidenceManifest]]:
    if not _is_digest(expected_digest):
        raise ValueError("invalid expected evidence digest")
    if expected_kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported expected evidence kind")

    root = _open_directory(path)
    chunks = -1
    try:
        try:
            entries = set(os.listdir(root))
        except OSError as exc:
            raise ValueError("could not list evidence directory") from exc
        if entries != _ROOT_ENTRIES:
            raise ValueError("evidence directory entries mismatch")

        manifest_payload = _read_regular_at(root, "manifest", max_bytes=MAX_MANIFEST_BYTES)
        complete_payload = _read_regular_at(root, "COMPLETE", max_bytes=0)
        if complete_payload != b"":
            raise ValueError("invalid evidence COMPLETE marker")
        manifest = _decode_manifest_payload(manifest_payload, expected_kind=expected_kind)
        if not hmac.compare_digest(manifest.digest, expected_digest):
            raise ValueError("evidence manifest digest mismatch")

        chunks = _open_directory_at(root, "chunks")
        try:
            chunk_entries = set(os.listdir(chunks))
        except OSError as exc:
            raise ValueError("could not list evidence chunks") from exc
        expected_names = {chunk.name for chunk in manifest.chunks}
        if chunk_entries != expected_names:
            raise ValueError("evidence chunk entries mismatch")
        yield root, chunks, manifest
    finally:
        if chunks >= 0:
            os.close(chunks)
        os.close(root)


def _iter_records_from_opened(
    chunks: int, manifest: EvidenceManifest
) -> Iterator[dict[str, object]]:
    root_digest = hashlib.sha256()
    observed_count = 0
    observed_total = 0

    for chunk in manifest.chunks:
        descriptor, initial_info = _open_regular_at(
            chunks, chunk.name, expected_bytes=chunk.byte_count
        )
        part_digest = hashlib.sha256()
        part_count = 0
        remaining = chunk.byte_count
        try:
            while remaining:
                if remaining < _FRAME_PREFIX_BYTES:
                    raise ValueError("evidence chunk has incomplete frame prefix")
                prefix = _read_exact(descriptor, _FRAME_PREFIX_BYTES)
                payload_length = int.from_bytes(prefix, "big")
                if payload_length > MAX_RECORD_PAYLOAD_BYTES:
                    raise ValueError("evidence record payload exceeds size limit")
                frame_size = _FRAME_PREFIX_BYTES + payload_length
                if frame_size > remaining:
                    raise ValueError("evidence frame exceeds declared chunk boundary")
                payload = _read_exact(descriptor, payload_length)
                try:
                    record = decode_row(payload)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ValueError("invalid evidence record codec payload") from exc
                frame = prefix + payload
                part_digest.update(frame)
                root_digest.update(frame)
                remaining -= frame_size
                observed_total += frame_size
                observed_count += 1
                part_count += 1
                if observed_total > MAX_ARTIFACT_BYTES:
                    raise ValueError("evidence artifact exceeds size limit")
                yield record

            if os.read(descriptor, 1):
                raise ValueError("evidence chunk has trailing bytes")
            final_info = os.fstat(descriptor)
            if final_info.st_size != initial_info.st_size:
                raise ValueError("evidence chunk changed while reading")
            if part_count != chunk.record_count:
                raise ValueError("evidence chunk record count mismatch")
            if not hmac.compare_digest(part_digest.hexdigest(), chunk.digest):
                raise ValueError("evidence chunk digest mismatch")
        except OSError as exc:
            raise ValueError("could not read evidence chunk") from exc
        finally:
            os.close(descriptor)

    if (
        observed_count != manifest.record_count
        or observed_total != manifest.total_bytes
        or not hmac.compare_digest(root_digest.hexdigest(), manifest.root_digest)
    ):
        raise ValueError("evidence root count or digest mismatch")


def iter_verified_records(
    path: Path, *, expected_digest: str, expected_kind: str
) -> Iterator[dict[str, object]]:
    """Stream verified records from a complete v2 artifact.

    The iterator is deliberately consuming: the final part, root count, and
    root digest checks run only after the caller reaches the end.
    """

    with _opened_artifact(path, expected_digest=expected_digest, expected_kind=expected_kind) as (
        _root,
        chunks,
        manifest,
    ):
        yield from _iter_records_from_opened(chunks, manifest)


def verify_artifact(
    path: Path, *, expected_digest: str, expected_kind: str
) -> EvidenceManifest:
    """Fully consume and verify an artifact, returning its typed manifest."""

    with _opened_artifact(path, expected_digest=expected_digest, expected_kind=expected_kind) as (
        _root,
        chunks,
        manifest,
    ):
        for _record in _iter_records_from_opened(chunks, manifest):
            pass
    return manifest


def encode_manifest(manifest: EvidenceManifest) -> bytes:
    """Encode a sealed manifest for focused protocol tests and consumers."""

    return _manifest_payload(manifest)


def decode_manifest(payload: bytes, *, expected_kind: str) -> EvidenceManifest:
    """Decode and validate one v2 manifest payload."""

    return _decode_manifest_payload(payload, expected_kind=expected_kind)


class EvidenceWriter:
    """Create one exclusive private v2 evidence directory."""

    def __init__(self, path: Path, header: _ManifestHeader, root: int, chunks: int) -> None:
        self._path = path
        self._header = header
        self._root = root
        self._chunks = chunks
        self._current: BinaryIO | None = None
        self._current_name: str | None = None
        self._current_count = 0
        self._current_bytes = 0
        self._current_digest = hashlib.sha256()
        self._chunk_descriptors: list[ChunkDescriptor] = []
        self._root_digest = hashlib.sha256()
        self._record_count = 0
        self._total_bytes = 0
        self._finished = False

    @classmethod
    def create(cls, path: Path, *, manifest_fields: Mapping[str, object]) -> EvidenceWriter:
        header = _normalize_header(manifest_fields)
        path.mkdir(mode=_DIRECTORY_MODE, exist_ok=False)
        path.chmod(_DIRECTORY_MODE)
        chunks_path = path / "chunks"
        chunks_path.mkdir(mode=_DIRECTORY_MODE, exist_ok=False)
        chunks_path.chmod(_DIRECTORY_MODE)
        root = _open_directory(path)
        try:
            chunks = _open_directory_at(root, "chunks")
        except Exception:
            os.close(root)
            raise
        return cls(path, header, root, chunks)

    def _sync_directory(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def _open_current_chunk(self) -> None:
        if len(self._chunk_descriptors) >= MAX_PART_COUNT:
            raise ValueError("evidence part count exceeds limit")
        name = f"{len(self._chunk_descriptors):08d}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, _FILE_MODE, dir_fd=self._chunks)
        try:
            os.fchmod(descriptor, _FILE_MODE)
            self._current = os.fdopen(descriptor, "wb", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        self._current_name = name
        self._current_count = 0
        self._current_bytes = 0
        self._current_digest = hashlib.sha256()

    def _close_current_chunk(self) -> None:
        current = self._current
        if current is None:
            return
        self._current = None
        try:
            current.flush()
            os.fsync(current.fileno())
        finally:
            current.close()
        name = self._current_name
        if name is None:
            raise ValueError("evidence writer chunk state invalid")
        self._chunk_descriptors.append(
            ChunkDescriptor(
                name=name,
                record_count=self._current_count,
                byte_count=self._current_bytes,
                digest=self._current_digest.hexdigest(),
            )
        )
        self._sync_directory(self._chunks)
        self._current_name = None

    def _write_file(self, name: str, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, _FILE_MODE, dir_fd=self._root)
        try:
            os.fchmod(descriptor, _FILE_MODE)
            with os.fdopen(descriptor, "wb", buffering=0) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            # fdopen owns the descriptor once entered; close only if it was
            # not transferred to the file object.
            with suppress(OSError):
                os.close(descriptor)
            raise
        self._sync_directory(self._root)

    def _build_manifest(self) -> EvidenceManifest:
        manifest = EvidenceManifest(
            kind=self._header.kind,
            format_version=self._header.format_version,
            run_id=self._header.run_id,
            scope=self._header.scope,
            image_digest=self._header.image_digest,
            projector_version=self._header.projector_version,
            stream=self._header.stream,
            record_kind=self._header.record_kind,
            record_count=self._record_count,
            total_bytes=self._total_bytes,
            chunks=tuple(self._chunk_descriptors),
            root_digest=self._root_digest.hexdigest(),
            digest="0" * 64,
            tables=self._header.tables,
            diagnostic_digest=self._header.diagnostic_digest,
            reviewer=self._header.reviewer,
        )
        return _seal_manifest(manifest)

    def append(self, record: Mapping[str, object]) -> None:
        if self._finished:
            raise ValueError("evidence writer is already finished")
        try:
            payload = encode_row(record)
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("invalid evidence record") from exc
        if len(payload) > MAX_RECORD_PAYLOAD_BYTES:
            raise ValueError("evidence record payload exceeds size limit")
        frame_size = _FRAME_PREFIX_BYTES + len(payload)
        if frame_size > MAX_PART_BYTES:
            raise ValueError("evidence record cannot fit in a part")
        if self._total_bytes + frame_size > MAX_ARTIFACT_BYTES:
            raise ValueError("evidence artifact exceeds size limit")
        if self._current is None:
            self._open_current_chunk()
        elif self._current_bytes + frame_size > MAX_PART_BYTES:
            self._close_current_chunk()
            self._open_current_chunk()

        frame = len(payload).to_bytes(_FRAME_PREFIX_BYTES, "big") + payload
        current = self._current
        if current is None:
            raise ValueError("evidence writer chunk state invalid")
        current.write(frame)
        self._current_digest.update(frame)
        self._root_digest.update(frame)
        self._current_count += 1
        self._current_bytes += frame_size
        self._record_count += 1
        self._total_bytes += frame_size

    def _close_descriptors(self) -> None:
        try:
            self._close_current_chunk()
        finally:
            if self._chunks >= 0:
                os.close(self._chunks)
                self._chunks = -1
            if self._root >= 0:
                os.close(self._root)
                self._root = -1

    def finish(self) -> EvidenceManifest:
        if self._finished:
            raise ValueError("evidence writer is already finished")
        try:
            self._close_current_chunk()
            manifest = self._build_manifest()
            payload = _manifest_payload(manifest)
            if len(payload) > MAX_MANIFEST_BYTES:
                raise ValueError("evidence manifest exceeds size limit")
            self._write_file("manifest", payload)
            self._write_file("COMPLETE", b"")
            self._finished = True
            return manifest
        finally:
            if self._finished or self._current is None:
                self._close_descriptors()
