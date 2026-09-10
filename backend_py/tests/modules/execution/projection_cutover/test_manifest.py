"""Manifest integrity must bind every field, including empty table schemas."""

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.projection_cutover import manifest as m
from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row
from bfx_funding_bot.modules.execution.projection_cutover.contracts import (
    ArchiveManifest,
    Scope,
    StreamIdentity,
)


def sample():
    return ArchiveManifest(
        UUID(int=1),
        Scope(UUID(int=2), "ci"),
        StreamIdentity(0, 0, hashlib.sha256(b"[]").hexdigest()),
        1,
        "sha256:synthetic",
        ("f8c2d4e6a901",),
        "execution-state-v1",
        tuple(
            {
                "name": name,
                "schema": [{"name": "id", "type": "BIGINT", "nullable": False}],
                "key_columns": ["id"],
                "count": 0,
                "digest": hashlib.sha256(b"").hexdigest(),
            }
            for name in m.TABLE_NAMES
        ),
        "",
    )


def test_length_delimited_digest_does_not_confuse_concatenated_rows():
    assert m.digest_rows([b"a", b"bc"]) != m.digest_rows([b"ab", b"c"])
    assert m.digest_rows([b"a"]) == hashlib.sha256(b"\x00\x00\x00\x00\x00\x00\x00\x01a").hexdigest()


def test_manifest_roundtrip_and_all_metadata_are_bound():
    original = m.seal_manifest(sample())
    assert m.decode_manifest(m.encode_manifest(original)) == original
    changes = {
        "run_id": UUID(int=3),
        "scope": Scope(UUID(int=4), "shadow"),
        "stream": StreamIdentity(1, 7, "f" * 64),
        "format_version": 2,
        "image_digest": "other",
        "migration_heads": ("unknown",),
        "projector_version": "other",
    }
    for field, value in changes.items():
        with pytest.raises(ValueError):
            m.validate_manifest(replace(original, **{field: value}))
    altered = [dict(entry) for entry in original.tables]
    altered[0]["schema"] = [{"name": "id", "type": "TEXT", "nullable": False}]
    with pytest.raises(ValueError):
        m.validate_manifest(replace(original, tables=tuple(altered)))


@pytest.mark.parametrize(
    "defect", ["missing", "duplicate", "order", "extra_field", "negative", "bool_count"]
)
def test_even_rehashed_invalid_manifest_is_rejected(defect):
    original = sample()
    entries = [dict(t) for t in original.tables]
    if defect == "missing":
        entries.pop()
    if defect == "duplicate":
        entries[-1] = entries[0]
    if defect == "order":
        entries.reverse()
    if defect == "extra_field":
        entries[0]["unexpected"] = True
    if defect == "negative":
        entries[0]["count"] = -1
    if defect == "bool_count":
        entries[0]["count"] = True
    with pytest.raises(ValueError):
        m.seal_manifest(replace(original, tables=tuple(entries)))


def test_archive_metadata_never_contaminates_sqlite_base():
    from bfx_funding_bot.core.db import Base
    from bfx_funding_bot.modules.execution.projection_cutover.tables import ArchiveBase

    assert set(ArchiveBase.metadata.tables) == {
        "projection_audit.runs",
        "projection_audit.rows",
        "projection_audit.receipts",
    }
    assert not any(t.schema == "projection_audit" for t in Base.metadata.tables.values())


def test_decoder_rejects_unknown_manifest_fields():
    original = m.seal_manifest(sample())
    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row

    raw = decode_row(m.encode_manifest(original))
    raw["secret"] = "unexpected"
    with pytest.raises(ValueError):
        m.decode_manifest(encode_row(raw))
