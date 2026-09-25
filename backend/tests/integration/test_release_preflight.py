"""Real migrated PostgreSQL and DR receipts through the typed release adapter."""
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, fields, replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile, CanaryStartupBlocked
from scripts.halt2_cutover import Halt2Evidence, collect_preflight_report
from scripts.run_canary_preflight import verify_release_preflight
from tests.integration.test_capital_repository import repository, setup_policy, snapshot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_typed_release_preflight_preserves_dr_schema_and_event_prefix(pg_engine, pg_container, tmp_path, monkeypatch):
    async with pg_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    result = subprocess.run(["uv", "run", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ, DATABASE_URL=url),
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    now = 1100
    monkeypatch.setattr("scripts.halt2_cutover.time.time", lambda: now / 1000)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="release-preflight-fixture"))
    capital = repository(account)
    await setup_policy(factory, capital)
    await snapshot(factory, capital)
    await HaltStateStore(factory, account_id=str(account), deployment_environment="ci").set_halted(True, reason="fixture", actor="fixture")
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    await TradingStateRepository(factory, account_id=account, deployment_environment="ci").transition(
        "HALTED", cause="operator", reason="fixture", actor="fixture")
    backup, restore, config = (tmp_path / name for name in ("backup.json", "restore.json", "safety.yaml"))
    backup.write_text(json.dumps({"schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 60, "observed_at_ms": now}))
    restore.write_text(json.dumps({"schema_version": 1, "kind": "restore", "measured": True, "rto_seconds": 30, "observed_at_ms": now}))
    backup.chmod(0o600)
    restore.chmod(0o600)
    config.write_text("fixture config")
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    async with factory() as session:
        report = await collect_preflight_report(session, account_id=account, environment="ci",
            artifact_hashes={"backup_evidence_hash": digest(backup), "isolated_restore_evidence_hash": digest(restore),
                "config_digest": digest(config), "backup_rpo_seconds": 60, "restore_rto_seconds": 30},
            image_digest="sha256:" + "a" * 64, projector_version="execution-state-v1")
    data = asdict(report)
    data.update(backup_evidence_path=str(backup), isolated_restore_evidence_path=str(restore), config_artifact_path=str(config))
    evidence = Halt2Evidence(**{field.name: data[field.name] for field in fields(Halt2Evidence)})
    from decimal import Decimal
    profile = CanaryProfile(account, "ci", "fUST", "a30", "mean_reversion", Decimal("153"), Decimal("200"), 300)
    async def verify(claim):
        async with factory() as session:
            return await verify_release_preflight(session=session, profile=profile, halt2_evidence=claim,
                config_artifact=config, image_digest="sha256:" + "a" * 64,
                projector_version="execution-state-v1", now_ms=now)
    assert await verify(evidence) is None
    # Reconciliation advances the stream after the restored baseline. The exact
    # original prefix remains mandatory; replacing it with current facts is not proof.
    await snapshot(factory, capital)
    assert await verify(evidence) is None
    profile = replace(profile, max_evidence_age_seconds=1)
    now = 4000
    with pytest.raises(CanaryStartupBlocked, match="venue_snapshot_stale"):
        await verify(evidence)
    profile = replace(profile, max_evidence_age_seconds=300)
    now = 1100
    for change, reason in (({"event_hash": "0" * 64}, "continuity"),
                           ({"image_digest": "sha256:" + "b" * 64}, "image_digest"),
                           ({"schema_heads": ("wrong",)}, "schema_heads")):
        with pytest.raises(CanaryStartupBlocked, match=reason):
            await verify(replace(evidence, **change))
    now = 400_000
    with pytest.raises(CanaryStartupBlocked, match="venue_snapshot_stale"):
        await verify(evidence)
    now = 1100
    restore.write_text(json.dumps({"schema_version": 1, "kind": "restore", "measured": True, "rto_seconds": 3601, "observed_at_ms": now}))
    with pytest.raises(CanaryStartupBlocked, match="restore_rto_exceeded"):
        await verify(replace(evidence, isolated_restore_evidence_hash=digest(restore), restore_rto_seconds=3601))
