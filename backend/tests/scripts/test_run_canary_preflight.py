"""Behavioral tests for the shared bounded-canary evidence verifier."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from scripts.halt2_cutover import Halt2Evidence
from scripts.run_canary_preflight import verify_canary_preflight


@pytest.mark.parametrize("release", [False, True])
def test_preflight_rejects_tampered_backup_before_readiness(monkeypatch, tmp_path: Path, release) -> None:
    """Changing a signed input after evidence creation must make the real verifier fail."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile, CanaryStartupBlocked

    backup = tmp_path / "backup.json"
    restore = tmp_path / "restore.json"
    config = tmp_path / "safety.yaml"
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1000)
    backup.write_text(json.dumps({"schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 60, "observed_at_ms": 1_000_000}))
    restore.write_text(json.dumps({"schema_version": 1, "kind": "restore", "measured": True, "rto_seconds": 30, "observed_at_ms": 1_000_000}))
    backup.chmod(0o600)
    restore.chmod(0o600)
    config.write_text("config-v1")
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    halt2 = Halt2Evidence(
        exchange_account_id="11111111-1111-1111-1111-111111111111",
        deployment_environment="prod",
        backup_evidence_hash=digest(backup),
        isolated_restore_evidence_hash=digest(restore),
        event_head=0,
        event_hash=hashlib.sha256(b"[]").hexdigest(),
        venue_snapshot_fence=None,
        venue_snapshot_observed_at_ms=None,
        venue_snapshot_complete=False,
        preflight_observed_at_ms=1_000_000,
        backup_rpo_seconds=60,
        restore_rto_seconds=30,
        config_digest=digest(config),
        image_digest="image-hash",
        projector_version="projector-v3",
        migration_head="head",
        schema_heads=("head",),
        backup_evidence_path=str(backup),
        isolated_restore_evidence_path=str(restore),
        config_artifact_path=str(config),
    )
    backup.write_text(json.dumps({"schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 60, "observed_at_ms": 1_000_000, "tampered": True}))
    profile = CanaryProfile(
        account_id=UUID(halt2.exchange_account_id), environment="prod", symbol="fUST",
        cell="fUST_a30", strategy="mean_reversion", amount_usdt=150,
        cap_usdt=150, max_evidence_age_seconds=300,
    )

    if release:
        from scripts.run_canary_preflight import verify_release_preflight
        with pytest.raises(CanaryStartupBlocked, match="halt2_artifact_evidence_mismatch"):
            asyncio.run(verify_release_preflight(session=object(), profile=profile,
                halt2_evidence=halt2, config_artifact=config, image_digest="image-hash",
                projector_version="projector-v3", now_ms=1_000_000))
        return
    with pytest.raises(CanaryStartupBlocked, match="halt2_artifact_evidence_mismatch"):
        asyncio.run(
            verify_canary_preflight(
                session=object(),
                profile=profile,
                halt2_evidence=halt2,
                config_artifact=config,
                image_digest="image-hash",
                projector_version="projector-v3",
                environ={},
                evidence=None,
                configured_cells=(("mean_reversion", "fUST", "fUST_a30"),),
                configured_caps={"fUST": 150},
                allocation_cap_usdt=150,
                now_ms=1_000_000,
            )
        )


def test_release_observation_survives_submit_expiry_but_requires_fresh_fences():
    from dataclasses import replace
    from decimal import Decimal

    from bfx_funding_bot.modules.marketfeed.daemon import (
        CanaryEvidence,
        CanaryProfile,
        CanaryReadiness,
        CanaryStartupBlocked,
        assert_release_observation,
    )
    profile = CanaryProfile(UUID("11111111-1111-1111-1111-111111111111"), "ci", "fUST",
                            "fUST_a30", "mean_reversion", Decimal("153"), Decimal("200"), 300)
    evidence = CanaryEvidence(str(profile.account_id), "ci", "fUST", "fUST_a30", "mean_reversion",
        Decimal("153"), "permit", "decision", "attempt", "acknowledged", "123", 1000, 2,
        (3, 4), (400000, 401000), "a" * 64, Decimal("0"), True, None)
    readiness = CanaryReadiness(0, 0, True, (3, 4), (400000, 401000), 402000, True)
    assert_release_observation(profile=profile, evidence=evidence, readiness=readiness)
    with pytest.raises(CanaryStartupBlocked, match="stale"):
        assert_release_observation(profile=profile, evidence=evidence,
                                   readiness=replace(readiness, observed_at_ms=800000))
    with pytest.raises(CanaryStartupBlocked, match="order"):
        assert_release_observation(profile=profile,
            evidence=replace(evidence, reconcile_fences=(3, 3)), readiness=readiness)


def test_canary_claim_cannot_override_server_derived_evidence() -> None:
    """The JSON report is a claim; the gate must compare it to DB-derived facts."""
    from dataclasses import replace

    from bfx_funding_bot.modules.marketfeed.daemon import CanaryEvidence, CanaryStartupBlocked
    from scripts.run_canary_preflight import _assert_claim_matches_server

    claim = CanaryEvidence(
        account_id="11111111-1111-1111-1111-111111111111",
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=150,
        permit_id="33333333-3333-3333-3333-333333333333",
        command_decision_id="decision-1",
        attempt_id="22222222-2222-2222-2222-222222222222",
        outcome_kind="acknowledged",
        venue_offer_id="offer-1",
        outcome_at_ms=1_000_000,
        outcome_event_seq=100,
        reconcile_fences=(101, 102),
        reconcile_observed_at_ms=(1_000_100, 1_000_200),
        projection_hash="a" * 64,
        venue_db_exposure_diff_usdt=0,
        full_account_snapshot_complete=True,
        stop_reason=None,
    )
    server = replace(claim, projection_hash="b" * 64)

    with pytest.raises(CanaryStartupBlocked, match="canary_evidence_not_server_derived"):
        _assert_claim_matches_server(claim, server)


@pytest.mark.parametrize("release", [False, True])
def test_preflight_names_the_stale_dr_measurement(monkeypatch, tmp_path: Path, release) -> None:
    """An expired DR receipt must name itself, not arrive as a bare ValueError.

    The 900s receipt bound is the one gate that expires on its own while every
    other input stays valid, so a session blocked by it has to say which
    measurement went stale.
    """
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile, CanaryStartupBlocked

    backup = tmp_path / "backup.json"
    restore = tmp_path / "restore.json"
    config = tmp_path / "safety.yaml"
    backup.write_text(json.dumps({"schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 60, "observed_at_ms": 1_000_000}))
    restore.write_text(json.dumps({"schema_version": 1, "kind": "restore", "measured": True, "rto_seconds": 30, "observed_at_ms": 1_000_000}))
    backup.chmod(0o600)
    restore.chmod(0o600)
    config.write_text("config-v1")
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    halt2 = Halt2Evidence(
        exchange_account_id="11111111-1111-1111-1111-111111111111",
        deployment_environment="prod",
        backup_evidence_hash=digest(backup),
        isolated_restore_evidence_hash=digest(restore),
        event_head=0,
        event_hash=hashlib.sha256(b"[]").hexdigest(),
        venue_snapshot_fence=None,
        venue_snapshot_observed_at_ms=None,
        venue_snapshot_complete=False,
        preflight_observed_at_ms=1_000_000,
        backup_rpo_seconds=60,
        restore_rto_seconds=30,
        config_digest=digest(config),
        image_digest="image-hash",
        projector_version="projector-v3",
        migration_head="head",
        schema_heads=("head",),
        backup_evidence_path=str(backup),
        isolated_restore_evidence_path=str(restore),
        config_artifact_path=str(config),
    )
    profile = CanaryProfile(
        account_id=UUID(halt2.exchange_account_id), environment="prod", symbol="fUST",
        cell="fUST_a30", strategy="mean_reversion", amount_usdt=150,
        cap_usdt=150, max_evidence_age_seconds=300,
    )
    # One second past the 900s receipt bound; nothing else about the evidence changed.
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: 1901)
    expected = "halt2_artifact_measurement:rpo_seconds_measurement_stale"

    if release:
        from scripts.run_canary_preflight import verify_release_preflight
        with pytest.raises(CanaryStartupBlocked, match=expected):
            asyncio.run(verify_release_preflight(session=object(), profile=profile,
                halt2_evidence=halt2, config_artifact=config, image_digest="image-hash",
                projector_version="projector-v3", now_ms=1_901_000))
        return
    with pytest.raises(CanaryStartupBlocked, match=expected):
        asyncio.run(
            verify_canary_preflight(
                session=object(),
                profile=profile,
                halt2_evidence=halt2,
                config_artifact=config,
                image_digest="image-hash",
                projector_version="projector-v3",
                environ={},
                evidence=None,
                configured_cells=(("mean_reversion", "fUST", "fUST_a30"),),
                configured_caps={"fUST": 150},
                allocation_cap_usdt=150,
                now_ms=1_901_000,
            )
        )
