"""Contract tests for the Halt 2 cutover preflight CLI.

The pure report verifier keeps these safety gates offline: no test opens a
production connection or contacts Bitfinex.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from scripts.halt2_cutover import (
    EXIT_PRECONDITION_FAILED,
    EXIT_SUCCESS,
    EXIT_VERIFICATION_FAILED,
    Halt2Evidence,
    PreflightReport,
    _read_dr_measurement,
    collect_preflight_report,
    main,
    verify_preflight,
)

ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")


@pytest.mark.parametrize("kind", [None, "archive_restore", "backup", "unknown"])
def test_full_restore_measurement_rejects_other_receipt_kinds(tmp_path, monkeypatch, kind):
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1000)
    path = tmp_path / "receipt.json"
    payload = {"schema_version": 1, "kind": kind, "measured": True,
               "rto_seconds": 30, "observed_at_ms": 1000000}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        _read_dr_measurement(path, key="rto_seconds")


@pytest.mark.parametrize("mutation", ["version", "bool-version", "duplicate", "oversized", "public", "symlink", "unmeasured"])
def test_receipt_reader_requires_private_bounded_versioned_evidence(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1000)
    path = tmp_path / "receipt.json"
    payload = {"schema_version": 1, "kind": "restore", "measured": True,
               "rto_seconds": 30, "observed_at_ms": 1000000}
    if mutation == "version":
        payload["schema_version"] = 2
    elif mutation == "bool-version":
        payload["schema_version"] = True
    elif mutation == "unmeasured":
        payload["measured"] = "true"
    raw = json.dumps(payload)
    if mutation == "duplicate":
        raw = raw[:-1] + ', "kind":"restore"}'
    if mutation == "oversized":
        raw += " " * 65536
    path.write_text(raw)
    path.chmod(0o644 if mutation == "public" else 0o600)
    if mutation == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    with pytest.raises(ValueError):
        _read_dr_measurement(path, key="rto_seconds")


@pytest.mark.parametrize("key", ["rpo_seconds", "rto_seconds"])
@pytest.mark.parametrize("observed", [None, True, 1000000.0, "1000000", -1, 1000001, "stale"])
def test_dr_freshness_rejects_missing_invalid_future_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, observed: object) -> None:
    # Far enough from the epoch that a day-old measurement is still a positive
    # timestamp: otherwise the stale case would be refused for being negative and
    # would pass whatever the freshness bound said.
    now_ms = 100_000_000
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: now_ms / 1000)
    path = tmp_path / "dr.json"
    report = {"schema_version": 1, "kind": "backup" if key == "rpo_seconds" else "restore", "measured": True, key: 30}
    if observed == "stale":
        # Just past this measurement's own window. Backup lag expires in minutes;
        # restore capability does not, and holding both to one clock is what forced
        # a fresh isolated restore before every authorisation.
        observed = now_ms - (900_001 if key == "rpo_seconds" else 86_400_001)
    elif observed == 1000001:
        observed = now_ms + 1          # still the "future" case
    elif observed == 1000000.0:
        observed = float(now_ms)       # still the "wrong type" case
    elif observed == "1000000":
        observed = str(now_ms)
    if observed is not None:
        report["observed_at_ms"] = observed
    path.write_text(json.dumps(report))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        _read_dr_measurement(path, key=key)


@pytest.mark.parametrize("key", ["rpo_seconds", "rto_seconds"])
@pytest.mark.parametrize("age_ms", [0, 900000, "own_bound"])
def test_dr_freshness_accepts_window_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, age_ms: object) -> None:
    """Each measurement is accepted right up to its own bound, not a shared one."""
    now_ms = 100_000_000
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: now_ms / 1000)
    if age_ms == "own_bound":
        age_ms = 900_000 if key == "rpo_seconds" else 86_400_000
    path = tmp_path / "dr.json"
    path.write_text(json.dumps({"schema_version": 1, "kind": "backup" if key == "rpo_seconds" else "restore", "measured": True, key: 30, "observed_at_ms": now_ms - age_ms}))
    path.chmod(0o600)
    assert _read_dr_measurement(path, key=key) == 30


@pytest.mark.parametrize("seconds", [True, 30.0, -1, "30.0", None])
def test_dr_freshness_preserves_strict_seconds_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: object) -> None:
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1000)
    path = tmp_path / "dr.json"
    path.write_text(json.dumps({"schema_version": 1, "kind": "restore", "measured": True, "rto_seconds": seconds, "observed_at_ms": 1000000}))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        _read_dr_measurement(path, key="rto_seconds")


def test_preflight_and_replay_use_the_same_nonempty_event_hash_contract() -> None:
    """A preflight hash must be accepted by the actual empty-projector replay."""
    from scripts.verify_projection_replay import replay_event_log

    row = EventLogRow(
        event_seq=7,
        account_id=str(ACCOUNT_ID),
        exchange_account_id=ACCOUNT_ID,
        deployment_environment="canary",
        event_type="CREDIT_CLOSED",
        cid=42,
        venue_offer_id="offer-7",
        venue_seq=9,
        event_id=UUID("bf0e94ae-3a03-4810-ae8f-b3c931531ce0"),
        schema_version=3,
        payload={
            "__schema_version__": 3,
            "__event_type__": "CREDIT_CLOSED",
            "account_id": str(ACCOUNT_ID),
            "symbol": "fUST",
            "amount": "1",
            "occurred_at_ms": 1007,
            "event_id": "bf0e94ae-3a03-4810-ae8f-b3c931531ce0",
        },
        occurred_at_ms=1007,
    )

    assert canonical_event_hash([row]) == replay_event_log(
        [row], account_id=ACCOUNT_ID, environment="canary",
    ).event_hash


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
        venue_snapshot_observed_at_ms=1_000_000,
        venue_snapshot_complete=True,
        preflight_observed_at_ms=1_000_250,
        backup_rpo_seconds=60,
        restore_rto_seconds=30,
        config_digest="config-hash",
        image_digest="image-hash",
        projector_version="projector-v3",
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
        venue_snapshot_fence=42,
        venue_snapshot_observed_at_ms=1_000_000,
        venue_snapshot_complete=True,
        preflight_observed_at_ms=1_000_250,
        backup_rpo_seconds=60,
        restore_rto_seconds=30,
        config_digest="config-hash",
        image_digest="image-hash",
        projector_version="projector-v3",
        migration_head="halt2-head",
        schema_heads=("halt2-head",),
        backup_evidence_path="/evidence/backup.json",
        isolated_restore_evidence_path="/evidence/restore.json",
        config_artifact_path="/evidence/config.yaml",
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


@pytest.mark.parametrize(
    ("restore_rto_seconds", "expected_exit_code", "expected_reasons"),
    [
        (60, EXIT_SUCCESS, ()),
        (61, EXIT_SUCCESS, ()),
        (3600, EXIT_SUCCESS, ()),
        (3601, EXIT_PRECONDITION_FAILED, ("restore_rto_exceeded",)),
    ],
)
def test_preflight_enforces_halt2_restore_rto_boundary(
    restore_rto_seconds: int,
    expected_exit_code: int,
    expected_reasons: tuple[str, ...],
) -> None:
    result = verify_preflight(
        _report(restore_rto_seconds=restore_rto_seconds),
        _evidence(restore_rto_seconds=restore_rto_seconds),
        environ={},
    )

    assert result.exit_code == expected_exit_code
    assert result.stop_reasons == expected_reasons


def test_preflight_rejects_unmeasured_dr_and_incomplete_snapshot() -> None:
    result = verify_preflight(
        _report(
            venue_snapshot_fence=None,
            venue_snapshot_observed_at_ms=None,
            venue_snapshot_complete=False,
            backup_rpo_seconds=None,
            restore_rto_seconds=3601,
        ),
        _evidence(
            venue_snapshot_fence=None,
            venue_snapshot_observed_at_ms=None,
            venue_snapshot_complete=False,
            backup_rpo_seconds=None,
            restore_rto_seconds=3601,
        ),
    )

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert "venue_snapshot_fence_absent" in result.stop_reasons
    assert "venue_snapshot_coverage_incomplete" in result.stop_reasons
    assert "backup_rpo_unmeasured" in result.stop_reasons
    assert "restore_rto_exceeded" in result.stop_reasons


@pytest.mark.parametrize("legacy_name", ["BFX_ACCOUNT_ID"])
def test_preflight_rejects_legacy_environment_variable(legacy_name: str) -> None:
    result = verify_preflight(_report(), _evidence(), environ={legacy_name: "legacy"})

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert result.stop_reasons == ("legacy_environment_variable:BFX_ACCOUNT_ID",)


def test_valid_read_only_report_has_no_mutation_and_passes() -> None:
    result = verify_preflight(
        _report(),
        _evidence(),
        environ={"BFX_EXCHANGE_ACCOUNT_ID": str(ACCOUNT_ID)},
    )

    assert result.exit_code == EXIT_SUCCESS
    assert result.stop_reasons == ()


@pytest.mark.parametrize(
    ("report_change", "expected_reason"),
    [
        ({"backup_evidence_hash": "observed-backup"}, "backup_evidence_hash_mismatch"),
        ({"isolated_restore_evidence_hash": "observed-restore"}, "isolated_restore_evidence_hash_mismatch"),
        ({"config_digest": "observed-config"}, "config_digest_mismatch"),
        ({"image_digest": "observed-image"}, "image_digest_mismatch"),
    ],
)
def test_preflight_rejects_evidence_not_matching_independent_artifact_or_runtime(
    report_change: dict[str, object], expected_reason: str
) -> None:
    result = verify_preflight(_report(**report_change), _evidence())

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert expected_reason in result.stop_reasons


@pytest.mark.parametrize(
    ("report_change", "expected_reason"),
    [
        ({"migration_head": "different-head"}, "migration_head_mismatch"),
        ({"schema_heads": ("different-head",)}, "schema_heads_mismatch"),
        ({"projector_version": "different-projector"}, "projector_version_mismatch"),
    ],
)
def test_preflight_rejects_migration_schema_or_projector_drift(
    report_change: dict[str, object], expected_reason: str
) -> None:
    result = verify_preflight(_report(**report_change), _evidence())

    assert result.exit_code == EXIT_PRECONDITION_FAILED
    assert expected_reason in result.stop_reasons


class _ReadOnlySession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def scalars(self, statement):  # type: ignore[no-untyped-def]
        self.statements.append(str(statement))
        return []

    async def scalar(self, statement):  # type: ignore[no-untyped-def]
        rendered = str(statement)
        self.statements.append(rendered)
        if "trading_halt.halted" in rendered:
            return True
        return 0 if "count" in rendered else None


def test_collect_preflight_uses_only_selects_and_never_mutates_db(monkeypatch) -> None:
    session = _ReadOnlySession()
    monkeypatch.setattr("scripts.halt2_cutover._migration_head", lambda: "halt2-head")

    report = asyncio.run(
        collect_preflight_report(
            session,  # type: ignore[arg-type]
            account_id=ACCOUNT_ID,
            environment="canary",
            artifact_hashes={
                "backup_evidence_hash": "backup-hash",
                "isolated_restore_evidence_hash": "restore-hash",
                "config_digest": "config-hash",
            },
            image_digest="image-hash",
            projector_version="projector-v3",
        )
    )

    assert report.persistent_halt is True
    assert session.statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in session.statements)


def test_main_returns_verification_unavailable_when_db_cannot_connect(monkeypatch, tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(asdict(_evidence())))
    monkeypatch.setattr("scripts.halt2_cutover.Settings", lambda: object())
    monkeypatch.setattr("scripts.halt2_cutover.make_engine", lambda _settings: (_ for _ in ()).throw(RuntimeError("db down")))

    assert main(["preflight", "--account-id", str(ACCOUNT_ID), "--environment", "canary", "--evidence", str(evidence_path), "--projector-version", "projector-v3", "--image-digest", "image-hash"]) == EXIT_VERIFICATION_FAILED


def test_main_rehashes_artifacts_and_rejects_tampered_backup(monkeypatch, tmp_path: Path) -> None:
    backup = tmp_path / "backup.json"
    restore = tmp_path / "restore.json"
    config = tmp_path / "safety.yaml"
    backup.write_text("backup-v1")
    restore.write_text("restore-v1")
    config.write_text("config-v1")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    evidence = _evidence(
        backup_evidence_hash=digest(backup),
        isolated_restore_evidence_hash=digest(restore),
        config_digest=digest(config),
        backup_evidence_path=str(backup),
        isolated_restore_evidence_path=str(restore),
        config_artifact_path=str(config),
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(asdict(evidence)))
    backup.write_text("backup-tampered")

    class _Engine:
        async def dispose(self) -> None:
            return None

    class _Session:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

    async def collect(_session, **kwargs):  # type: ignore[no-untyped-def]
        return _report(
            backup_evidence_hash=kwargs["artifact_hashes"]["backup_evidence_hash"],
            isolated_restore_evidence_hash=kwargs["artifact_hashes"]["isolated_restore_evidence_hash"],
            config_digest=kwargs["artifact_hashes"]["config_digest"],
        )

    monkeypatch.setattr("scripts.halt2_cutover.Settings", lambda: object())
    monkeypatch.setattr("scripts.halt2_cutover.make_engine", lambda _settings: _Engine())
    monkeypatch.setattr("scripts.halt2_cutover.make_session_factory", lambda _engine: _Session)
    monkeypatch.setattr("scripts.halt2_cutover.collect_preflight_report", collect)

    assert main([
        "preflight", "--account-id", str(ACCOUNT_ID), "--environment", "canary",
        "--evidence", str(evidence_path), "--projector-version", "projector-v3",
        "--image-digest", "image-hash", "--config-artifact", str(config),
    ]) == EXIT_PRECONDITION_FAILED


def test_assert_halt_uses_existing_store_only(monkeypatch, tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(asdict(_evidence())))
    calls: list[tuple[bool, str, str]] = []

    class _Engine:
        async def dispose(self) -> None:
            return None

    class _Store:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def set_halted(self, halted: bool, *, reason: str, actor: str):
            calls.append((halted, reason, actor))
            return SimpleNamespace(halted=True)

    monkeypatch.setattr("scripts.halt2_cutover.make_engine", lambda _settings: _Engine())
    monkeypatch.setattr("scripts.halt2_cutover.make_session_factory", lambda _engine: object())
    monkeypatch.setattr("scripts.halt2_cutover.HaltStateStore", _Store)
    monkeypatch.setattr("scripts.halt2_cutover.Settings", lambda: object())

    assert main(["assert-halt", "--account-id", str(ACCOUNT_ID), "--environment", "canary", "--evidence", str(evidence_path), "--projector-version", "projector-v3", "--image-digest", "image-hash", "--operator-id", "operator-1", "--reason", "test halt"]) == EXIT_SUCCESS
    assert calls == [(True, "test halt", "operator-1")]


@pytest.mark.parametrize("command", ["replay", "verify", "release-report", "convert-pending", "quarantine"])
def test_unowned_cutover_commands_are_explicitly_rejected(command: str, tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(asdict(_evidence())))

    assert main([command, "--account-id", str(ACCOUNT_ID), "--environment", "canary", "--evidence", str(evidence_path), "--projector-version", "projector-v3", "--image-digest", "image-hash"]) == EXIT_PRECONDITION_FAILED
