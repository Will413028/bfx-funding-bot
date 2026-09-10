"""Archive verifier contracts use independent inputs, never restored manifests."""

import importlib

import pytest


def test_explicit_empty_transport_decodes_for_legacy_inventory_check():
    module = importlib.import_module("scripts.verify_projection_archive")
    assert module.decode_archive_inputs(b'{"schema_version":1,"prepared":[]}') == ()


@pytest.mark.parametrize("raw", [
    b"{}", b'{"schema_version":true}', b"x" * 1048577,
    b'{"schema_version":2,"prepared":[]}',
    b'{"schema_version":3,"prepared":[]}',
    b'{"schema_version":2,"target_run_id":null,"prepared":[]}',
    b'{"schema_version":2,"target_run_id":true,"prepared":[]}',
    b'{"schema_version":2,"target_run_id":"00000000-0000-0000-0000-000000000123","prepared":[]}',
    b'{"schema_version":1,"schema_version":1,"prepared":[]}',
    b'{"schema_version":1,"prepared":{}}',
    b'{"schema_version":1,"prepared":[{"sha256":"a","payload":"%%%"}]}',
], ids=["missing", "bool-version", "oversized", "missing-target", "unsupported", "null-target",
        "bool-target", "absent-target", "duplicate", "mapping", "base64"])
def test_archive_input_rejects_missing_version_inventory_and_oversize(raw):
    module = importlib.import_module("scripts.verify_projection_archive")
    with pytest.raises(ValueError):
        module.decode_archive_inputs(raw)
