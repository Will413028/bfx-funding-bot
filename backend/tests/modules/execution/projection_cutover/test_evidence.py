import hashlib
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.projection_cutover import evidence
from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row, encode_row
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope, StreamIdentity

ACCOUNT = UUID(int=1)
SCOPE = Scope(ACCOUNT, "ci")
STREAM = StreamIdentity(3, 7, "e" * 64)
DIAGNOSTIC_KIND = "projection-cutover-diagnostic-v2"
CLASSIFICATION_KIND = "projection-cutover-classification-v2"


def _header(*, kind: str = DIAGNOSTIC_KIND, record_kind: str = "difference") -> dict[str, object]:
    fields: dict[str, object] = {
        "kind": kind,
        "format_version": 2,
        "run_id": UUID(int=2),
        "scope": SCOPE,
        "image_digest": "sha256:image",
        "projector_version": "execution-state-v1",
        "stream": STREAM,
        "record_kind": record_kind,
    }
    if kind == DIAGNOSTIC_KIND:
        fields["tables"] = (
            {"name": "position_state", "count": 1, "digest": "a" * 64},
        )
    else:
        fields["diagnostic_digest"] = "d" * 64
        fields["reviewer"] = "reviewer-1"
    return fields


def _record(number: int = 1) -> dict[str, object]:
    return {
        "table": "position_state",
        "key_digest": f"{number:064x}",
        "column": "last_updated_ms",
        "before_digest": "b" * 64,
        "after_digest": "c" * 64,
        "classification": "unexplained",
    }


def _manifest_without_digest(raw: bytes) -> dict[str, object]:
    fields = decode_row(raw)
    fields.pop("digest")
    return fields


def _rewrite_manifest(path: Path, fields: dict[str, object]) -> str:
    digest = hashlib.sha256(encode_row(fields)).hexdigest()
    path.joinpath("manifest").write_bytes(encode_row({**fields, "digest": digest}))
    return digest


def _frame_bytes(records: list[dict[str, object]]) -> bytes:
    return b"".join(
        len(payload).to_bytes(8, "big") + payload
        for payload in (encode_row(record) for record in records)
    )


def test_writer_round_trip_and_digest(tmp_path: Path) -> None:
    writer = evidence.EvidenceWriter.create(
        tmp_path / "diagnostic", manifest_fields=_header()
    )
    writer.append(_record())
    manifest = writer.finish()

    assert isinstance(manifest, evidence.EvidenceManifest)
    assert manifest.record_count == 1
    assert manifest.total_bytes == len(_frame_bytes([_record()]))
    verified_records = list(
        evidence.iter_verified_records(
            tmp_path / "diagnostic",
            expected_digest=manifest.digest,
            expected_kind=DIAGNOSTIC_KIND,
        )
    )
    assert verified_records[0]["table"] == "position_state"
    assert stat.S_IMODE((tmp_path / "diagnostic").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "diagnostic" / "chunks").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "diagnostic" / "manifest").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "diagnostic" / "COMPLETE").stat().st_mode) == 0o600
    assert manifest.chunks[0].name == "00000000.part"
    assert stat.S_IMODE(
        (tmp_path / "diagnostic" / "chunks" / manifest.chunks[0].name).stat().st_mode
    ) == 0o600


def test_empty_artifact_has_stable_empty_root(tmp_path: Path) -> None:
    manifest = evidence.EvidenceWriter.create(
        tmp_path / "empty", manifest_fields=_header()
    ).finish()

    assert manifest.record_count == 0
    assert manifest.total_bytes == 0
    assert manifest.chunks == ()
    assert manifest.root_digest == hashlib.sha256(b"").hexdigest()
    assert list(
        evidence.iter_verified_records(
            tmp_path / "empty", expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND
        )
    ) == []


def test_deterministic_manifest_root_part_and_file_bytes(tmp_path: Path) -> None:
    records = [_record(1), _record(2)]
    first = evidence.EvidenceWriter.create(tmp_path / "first", manifest_fields=_header())
    second = evidence.EvidenceWriter.create(tmp_path / "second", manifest_fields=_header())
    for record in records:
        first.append(record)
        second.append(dict(reversed(tuple(record.items()))))
    first_manifest = first.finish()
    second_manifest = second.finish()

    assert first_manifest == second_manifest
    assert (tmp_path / "first" / "manifest").read_bytes() == (
        tmp_path / "second" / "manifest"
    ).read_bytes()
    for descriptor in first_manifest.chunks:
        assert (tmp_path / "first" / "chunks" / descriptor.name).read_bytes() == (
            tmp_path / "second" / "chunks" / descriptor.name
        ).read_bytes()


def test_part_descriptors_hash_exact_frames_and_rotate_before_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_PART_BYTES", 600)
    records = [_record(1), _record(2), _record(3)]
    writer = evidence.EvidenceWriter.create(tmp_path / "parts", manifest_fields=_header())
    for record in records:
        writer.append(record)
    manifest = writer.finish()

    assert len(manifest.chunks) == 3
    for index, record in enumerate(records):
        expected = _frame_bytes([record])
        descriptor = manifest.chunks[index]
        assert descriptor.byte_count == len(expected)
        assert descriptor.record_count == 1
        assert descriptor.digest == hashlib.sha256(expected).hexdigest()
        assert (tmp_path / "parts" / "chunks" / descriptor.name).read_bytes() == expected
    assert manifest.root_digest == hashlib.sha256(_frame_bytes(records)).hexdigest()
    assert manifest.total_bytes == sum(chunk.byte_count for chunk in manifest.chunks)


@pytest.mark.parametrize(
    ("kind", "record_kind"),
    [
        ("projection-cutover-diagnostic-v1", "difference"),
        (DIAGNOSTIC_KIND, "classification"),
        (CLASSIFICATION_KIND, "difference"),
        ("unknown", "unknown"),
    ],
)
def test_create_rejects_unsupported_kind_or_record_kind_before_directory_creation(
    tmp_path: Path, kind: str, record_kind: str
) -> None:
    path = tmp_path / "rejected"
    with pytest.raises(ValueError):
        evidence.EvidenceWriter.create(
            path, manifest_fields=_header(kind=kind, record_kind=record_kind)
        )
    assert not path.exists()


def test_classification_header_is_typed_and_round_trips(tmp_path: Path) -> None:
    writer = evidence.EvidenceWriter.create(
        tmp_path / "classification", manifest_fields=_header(kind=CLASSIFICATION_KIND, record_kind="classification")
    )
    writer.append({"table": "position_state", "classification": "historical_state"})
    manifest = writer.finish()

    assert manifest.kind == CLASSIFICATION_KIND
    assert manifest.record_kind == "classification"
    assert manifest.diagnostic_digest == "d" * 64
    assert manifest.reviewer == "reviewer-1"
    assert evidence.verify_artifact(
        tmp_path / "classification",
        expected_digest=manifest.digest,
        expected_kind=CLASSIFICATION_KIND,
    ) == manifest


def test_append_rejects_oversized_record_without_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "MAX_RECORD_PAYLOAD_BYTES", 32)
    writer = evidence.EvidenceWriter.create(tmp_path / "oversized", manifest_fields=_header())
    with pytest.raises(ValueError, match="record payload"):
        writer.append(_record())
    assert not (tmp_path / "oversized" / "COMPLETE").exists()
    assert list((tmp_path / "oversized" / "chunks").iterdir()) == []


def test_append_rejects_artifact_limit_before_writing_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "MAX_ARTIFACT_BYTES", 10)
    writer = evidence.EvidenceWriter.create(tmp_path / "limited", manifest_fields=_header())
    with pytest.raises(ValueError, match="artifact"):
        writer.append(_record())
    assert list((tmp_path / "limited" / "chunks").iterdir()) == []


def test_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "existing"
    path.mkdir()
    with pytest.raises(FileExistsError):
        evidence.EvidenceWriter.create(path, manifest_fields=_header())

    link = tmp_path / "link"
    link.symlink_to(path, target_is_directory=True)
    with pytest.raises(FileExistsError):
        evidence.EvidenceWriter.create(link, manifest_fields=_header())


def test_writer_lifecycle_rejects_append_and_finish_after_finish(tmp_path: Path) -> None:
    writer = evidence.EvidenceWriter.create(tmp_path / "lifecycle", manifest_fields=_header())
    writer.append(_record())
    writer.finish()
    with pytest.raises(ValueError):
        writer.append(_record(2))
    with pytest.raises(ValueError):
        writer.finish()


def test_reader_rejects_expected_digest_or_kind_mismatch(tmp_path: Path) -> None:
    manifest = evidence.EvidenceWriter.create(
        tmp_path / "artifact", manifest_fields=_header()
    ).finish()
    with pytest.raises(ValueError, match="digest"):
        evidence.verify_artifact(
            tmp_path / "artifact", expected_digest="0" * 64, expected_kind=DIAGNOSTIC_KIND
        )
    with pytest.raises(ValueError, match="kind"):
        evidence.verify_artifact(
            tmp_path / "artifact", expected_digest=manifest.digest, expected_kind=CLASSIFICATION_KIND
        )


def test_reader_rejects_missing_complete_marker(tmp_path: Path) -> None:
    path = tmp_path / "incomplete"
    writer = evidence.EvidenceWriter.create(path, manifest_fields=_header())
    writer.append(_record())
    writer.finish()
    (path / "COMPLETE").unlink()
    with pytest.raises(ValueError):
        evidence.verify_artifact(
            path, expected_digest="0" * 64, expected_kind=DIAGNOSTIC_KIND
        )


def test_reader_rejects_tampered_frame_and_trailing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "tampered"
    manifest = evidence.EvidenceWriter.create(path, manifest_fields=_header())
    manifest.append(_record())
    sealed = manifest.finish()
    part = path / "chunks" / sealed.chunks[0].name

    part.write_bytes(part.read_bytes() + b"trailing")
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=sealed.digest, expected_kind=DIAGNOSTIC_KIND)

    part.write_bytes(_frame_bytes([{"not": "the original record"}]))
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=sealed.digest, expected_kind=DIAGNOSTIC_KIND)


@pytest.mark.parametrize("target", ["manifest", "COMPLETE", "chunks"])
def test_reader_rejects_symlinked_required_entries(tmp_path: Path, target: str) -> None:
    path = tmp_path / "symlinked"
    manifest = evidence.EvidenceWriter.create(path, manifest_fields=_header()).finish()
    outside = tmp_path / f"outside-{target}"
    if target == "chunks":
        outside.mkdir()
        path.joinpath(target).rmdir()
    else:
        outside.write_bytes(b"")
        path.joinpath(target).unlink()
    path.joinpath(target).symlink_to(outside, target_is_directory=target == "chunks")
    with pytest.raises((OSError, ValueError)):
        evidence.verify_artifact(path, expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_symlinked_part(tmp_path: Path) -> None:
    writer = evidence.EvidenceWriter.create(
        tmp_path / "part-link-real", manifest_fields=_header()
    )
    writer.append(_record())
    sealed = writer.finish()
    real_path = tmp_path / "part-link-real"
    part = real_path / "chunks" / sealed.chunks[0].name
    outside = tmp_path / "outside.part"
    outside.write_bytes(part.read_bytes())
    part.unlink()
    part.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        evidence.verify_artifact(real_path, expected_digest=sealed.digest, expected_kind=DIAGNOSTIC_KIND)


@pytest.mark.parametrize(
    ("relative", "mode"),
    [("root", 0o750), ("chunks", 0o750), ("manifest", 0o640), ("COMPLETE", 0o640)],
)
def test_reader_rejects_wrong_modes(tmp_path: Path, relative: str, mode: int) -> None:
    path = tmp_path / "modes"
    manifest = evidence.EvidenceWriter.create(path, manifest_fields=_header()).finish()
    (path if relative == "root" else path.joinpath(relative)).chmod(mode)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "owner"
    manifest = evidence.EvidenceWriter.create(path, manifest_fields=_header()).finish()
    current_uid = os.getuid()
    monkeypatch.setattr(evidence.os, "getuid", lambda: current_uid + 1)
    with pytest.raises(ValueError, match="owner"):
        evidence.verify_artifact(path, expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_extra_entries_and_missing_or_unlisted_parts(tmp_path: Path) -> None:
    path = tmp_path / "entries"
    manifest = evidence.EvidenceWriter.create(path, manifest_fields=_header()).finish()
    (path / "extra").touch(mode=0o600)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND)

    (path / "extra").unlink()
    (path / "chunks" / "00000000.part").write_bytes(b"")
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=manifest.digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_non_lexical_or_duplicate_manifest_parts(tmp_path: Path) -> None:
    path = tmp_path / "manifest-parts"
    writer = evidence.EvidenceWriter.create(path, manifest_fields=_header())
    writer.append(_record(1))
    writer.append(_record(2))
    writer.finish()
    fields = _manifest_without_digest((path / "manifest").read_bytes())
    chunks = list(fields["chunks"])
    assert len(chunks) == 1
    chunks[0]["name"] = "00000001.part"  # type: ignore[index]
    fields["chunks"] = chunks
    new_digest = _rewrite_manifest(path, fields)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=new_digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_manifest_count_digest_and_artifact_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest-limits"
    writer = evidence.EvidenceWriter.create(path, manifest_fields=_header())
    writer.append(_record())
    original = writer.finish()
    fields = _manifest_without_digest((path / "manifest").read_bytes())
    fields["record_count"] = 2
    digest = _rewrite_manifest(path, fields)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=digest, expected_kind=DIAGNOSTIC_KIND)

    fields = _manifest_without_digest((path / "manifest").read_bytes())
    fields["record_count"] = original.record_count
    fields["total_bytes"] = 1
    digest = _rewrite_manifest(path, fields)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=digest, expected_kind=DIAGNOSTIC_KIND)

    monkeypatch.setattr(evidence, "MAX_ARTIFACT_BYTES", 0)
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=digest, expected_kind=DIAGNOSTIC_KIND)


def test_reader_rejects_untrusted_oversized_frame_length(tmp_path: Path) -> None:
    path = tmp_path / "oversized-frame"
    writer = evidence.EvidenceWriter.create(path, manifest_fields=_header())
    writer.append(_record())
    sealed = writer.finish()
    part = path / "chunks" / sealed.chunks[0].name
    part.write_bytes((evidence.MAX_RECORD_PAYLOAD_BYTES + 1).to_bytes(8, "big"))
    with pytest.raises(ValueError):
        evidence.verify_artifact(path, expected_digest=sealed.digest, expected_kind=DIAGNOSTIC_KIND)


def test_manifest_and_summary_dataclasses_are_frozen_and_slot_based(tmp_path: Path) -> None:
    manifest = evidence.EvidenceWriter.create(
        tmp_path / "types", manifest_fields=_header()
    ).finish()
    summary = evidence.VerifiedCutoverEvidence(manifest, manifest, (("historical_state", 1),))
    assert not hasattr(manifest, "__dict__")
    assert not hasattr(summary, "__dict__")
    with pytest.raises(FrozenInstanceError):
        manifest.kind = "changed"  # type: ignore[misc]
