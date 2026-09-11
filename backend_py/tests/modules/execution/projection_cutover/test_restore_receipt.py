"""Archive restore identity and the DR 900-second acceptance window."""

import pytest

from bfx_funding_bot.modules.execution.projection_cutover import apply
from bfx_funding_bot.modules.execution.projection_cutover.manifest import TABLE_NAMES, seal_manifest
from tests.modules.execution.projection_cutover.test_manifest import sample


@pytest.fixture
def restore_proof():
    expected = seal_manifest(sample())
    scope = {"account_id": str(expected.scope.account_id), "environment": "ci"}
    common = {
        "event_count": 0, "event_head": None, "event_hash": expected.stream.digest,
        "migration_heads": ["f8c2d4e6a901"],
    }
    target = {
        "run_id": str(expected.run_id), "scope": scope, "manifest_digest": expected.digest,
        "verified_tables": list(TABLE_NAMES),
        "verified_counts": dict.fromkeys(TABLE_NAMES, 0),
        "verified_digests": dict.fromkeys(TABLE_NAMES, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        "original_event_count": 0, "original_event_head": 0,
        "original_event_hash": expected.stream.digest, "image_digest": expected.image_digest,
        "projector_version": expected.projector_version, "migration_heads": ["f8c2d4e6a901"],
        "prepared_digest": "d" * 64,
    }
    return expected, {
        **common, **scope, "schema_version": 2, "kind": "archive_restore", "measured": True,
        "target_run_id": str(expected.run_id), "projector_version": expected.projector_version,
        "verifier_image_digest": expected.image_digest, "network_internal": True,
        "egress_disconnected": True, "verifier_exit_status": 0,
        "archive_input_digest": "a" * 64, "config_digest": "b" * 64,
        "restore_run_id": "synthetic-restore", "target_backup_label": "synthetic-backup",
        "network_name": "synthetic-isolated", "observed_at_ms": 1_000_000,
        "rto_seconds": 10, "server_version_num": 180000,
        "image_digest": "sha256:" + "c" * 64, "image_labels": {}, "target_time": None,
        "archive_verification": {
            **common, "schema_version": 2, "archive_only": True,
            "target_run_id": str(expected.run_id), "scope": scope, "archives": [target],
        },
    }


@pytest.mark.parametrize("age_ms", [-1, 900_001])
def test_restore_receipt_rejects_future_and_stale_proof(restore_proof, age_ms):
    expected, receipt = restore_proof
    with pytest.raises(ValueError, match="restore_receipt_time_invalid"):
        apply.validate_restore_receipt(expected, receipt, now_ms=1_000_000 + age_ms)


@pytest.mark.parametrize("age_ms", [0, 1, 899_999, 900_000])
def test_restore_receipt_accepts_closed_window_boundaries(restore_proof, age_ms):
    expected, receipt = restore_proof
    apply.validate_restore_receipt(expected, receipt, now_ms=1_000_000 + age_ms)


@pytest.mark.parametrize("now_ms", [True, 1_000_000.0, -1, None])
def test_restore_receipt_rejects_invalid_clock(restore_proof, now_ms):
    expected, receipt = restore_proof
    with pytest.raises(ValueError, match="restore_receipt_time_invalid"):
        apply.validate_restore_receipt(expected, receipt, now_ms=now_ms)


@pytest.mark.parametrize("mutation", ["kind", "timestamp", "scope", "digest", "oversized"])
def test_restore_receipt_freshness_never_bypasses_identity(restore_proof, mutation):
    expected, receipt = restore_proof
    if mutation == "kind":
        receipt["kind"] = "restore"
    elif mutation == "timestamp":
        receipt["observed_at_ms"] = True
    elif mutation == "scope":
        receipt["environment"] = "other"
    elif mutation == "digest":
        receipt["archive_verification"]["archives"][0]["manifest_digest"] = "0" * 64
    else:
        receipt["extra"] = "x" * 131072
    with pytest.raises(ValueError, match="restore_"):
        apply.validate_restore_receipt(expected, receipt, now_ms=1_000_000)
