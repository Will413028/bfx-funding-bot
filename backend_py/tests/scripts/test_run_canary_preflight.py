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


def test_preflight_rejects_tampered_backup_before_readiness(monkeypatch, tmp_path: Path) -> None:
    """Changing a signed input after evidence creation must make the real verifier fail."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile, CanaryStartupBlocked

    backup = tmp_path / "backup.json"
    restore = tmp_path / "restore.json"
    config = tmp_path / "safety.yaml"
    backup.write_text(json.dumps({"measured": True, "rpo_seconds": 60}))
    restore.write_text(json.dumps({"measured": True, "rto_seconds": 30}))
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
    backup.write_text(json.dumps({"measured": True, "rpo_seconds": 60, "tampered": True}))
    profile = CanaryProfile(
        account_id=UUID(halt2.exchange_account_id), environment="prod", symbol="fUST",
        cell="fUST_a30", strategy="mean_reversion", amount_usdt=150,
        cap_usdt=150, max_evidence_age_seconds=300,
    )

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
