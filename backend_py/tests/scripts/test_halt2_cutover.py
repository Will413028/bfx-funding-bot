"""Contract tests for the Halt 2 cutover preflight CLI.

The pure report verifier keeps these safety gates offline: no test opens a
production connection or contacts Bitfinex.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from scripts.halt2_cutover import (
    EXIT_PRECONDITION_FAILED,
    EXIT_SUCCESS,
    Halt2Evidence,
    PreflightReport,
    verify_preflight,
)

ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")


def _report(**changes: object) -> PreflightReport:
    report = PreflightReport(
        exchange_account_id=str(ACCOUNT_ID),
        deployment_environment="canary",
        migration_head="halt2-head",
        schema_heads=("halt2-head",),
        backup_evidence_hash="backup-hash",
        isolated_restore_evidence_hash="restore-hash",
        event_count=4,
        event_head=42,
        event_hash="event-hash",
        open_uncertainty_count=0,
        venue_snapshot_fence=42,
        config_digest="config-hash",
        image_digest="image-hash",
        persistent_halt=True,
        stop_reasons=(),
    )
    return replace(report, **changes)


def _evidence(**changes: object) -> Halt2Evidence:
    evidence = Halt2Evidence(
        exchange_account_id=str(ACCOUNT_ID),
        deployment_environment="canary",
        backup_evidence_hash="backup-hash",
        isolated_restore_evidence_hash="restore-hash",
        event_head=42,
        event_hash="event-hash",
        config_digest="config-hash",
        image_digest="image-hash",
        projector_version="projector-v3",
    )
    return replace(evidence, **changes)


def test_preflight_rejects_missing_evidence() -> None:
    result = verify_preflight(_report(), None)

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert result.stop_reasons == ("missing_evidence",)


def test_preflight_rejects_stale_event_hash() -> None:
    result = verify_preflight(_report(), _evidence(event_hash="stale"))

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert result.stop_reasons == ("event_hash_mismatch",)


def test_preflight_rejects_open_unresolved_uncertainty() -> None:
    result = verify_preflight(_report(open_uncertainty_count=1), _evidence())

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert "open_execution_uncertainty" in result.stop_reasons


def test_preflight_rejects_wrong_account_uuid() -> None:
    result = verify_preflight(
        _report(),
        _evidence(exchange_account_id="28b31e79-83ce-4b32-a6b7-03d78043ce68"),
    )

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert result.stop_reasons == ("exchange_account_id_mismatch",)


def test_preflight_rejects_absent_persistent_halt() -> None:
    result = verify_preflight(_report(persistent_halt=False), _evidence())

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert "persistent_halt_absent" in result.stop_reasons


@pytest.mark.parametrize("legacy_name", ["BFX_ACCOUNT_ID"])
def test_preflight_rejects_legacy_environment_variable(legacy_name: str) -> None:
    result = verify_preflight(_report(), _evidence(), environ={legacy_name: "legacy"})

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert result.stop_reasons == ("legacy_environment_variable:BFX_ACCOUNT_ID",)


def test_valid_read_only_report_has_no_mutation_and_passes() -> None:
    mutations: list[str] = []
    result = verify_preflight(
        _report(),
        _evidence(),
        environ={"BFX_EXCHANGE_ACCOUNT_ID": str(ACCOUNT_ID)},
        mutation_probe=mutations.append,
    )

    assert result.exit_code == EXIT_SUCCESS
    assert result.stop_reasons == ()
    assert mutations == []
