"""Behavioral tests for the shared bounded-canary evidence verifier."""
from __future__ import annotations

import asyncio
import hashlib
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
    backup.write_text("backup-v1")
    restore.write_text("restore-v1")
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
        config_digest=digest(config),
        image_digest="image-hash",
        projector_version="projector-v3",
        migration_head="head",
        schema_heads=("head",),
        backup_evidence_path=str(backup),
        isolated_restore_evidence_path=str(restore),
        config_artifact_path=str(config),
    )
    backup.write_text("backup-tampered")
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
