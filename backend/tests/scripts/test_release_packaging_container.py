"""Actual image checks; no venue/network access at runtime."""
import asyncio
import base64
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import pytest

import bfx_funding_bot.modules.execution.capital_tables
import bfx_funding_bot.modules.execution.release_tables
import bfx_funding_bot.modules.marketfeed.daemon  # noqa: F401
from bfx_funding_bot.core.release_identity import ReleaseManifest
from scripts.release_package import MEASURE, PackagingBlocked, create_launch, run, run_one_shot

# Same full hash functions and same PG account lock as ReleaseWorker. No session
# transition, permit, venue, worker or financial command is executed here.
LOCK_MEASUREMENT = """
import asyncio,json,os,time
from pathlib import Path
from uuid import UUID
from sqlalchemy import text
from bfx_funding_bot.core.release_identity import measure_inventory,measure_python_inventory,canonical_digest
from bfx_funding_bot.core.db import make_async_engine_from_url,make_session_factory
from bfx_funding_bot.modules.execution.release_session import ReleaseSessions
async def main():
    engine=make_async_engine_from_url(os.environ['DATABASE_URL'])
    factory=make_session_factory(engine)
    repo=ReleaseSessions(UUID(os.environ['BFX_EXCHANGE_ACCOUNT_ID']),os.environ['BFX_DEPLOYMENT_ENV'])
    manifest=json.loads(Path('/run/bfx-release/manifest.json').read_bytes())
    def verify_inventory():
        assert measure_inventory(Path('/app'),require_protected=True)==manifest['inventory']
        assert measure_python_inventory(Path('/usr/local'),require_protected=True)==manifest['python_inventory']
        return canonical_digest(manifest)
    async with factory() as warm: await warm.execute(text('SELECT 1'))
    async def contender():
        async with factory.begin() as other:
            started=time.perf_counter()
            await repo.lock(other)
            return time.perf_counter()-started
    results=[]
    for _ in range(3):
        async with factory.begin() as session:
            started=time.perf_counter()
            await repo.lock(session)
            acquired=time.perf_counter()
            waiter=asyncio.create_task(contender())
            # A transition can verify in binding_reader and again in readiness.
            digest=await asyncio.to_thread(verify_inventory)
            first=time.perf_counter()
            await asyncio.to_thread(verify_inventory)
            second=time.perf_counter()
            assert not waiter.done(), 'account lock did not serialize'
        released=time.perf_counter()
        waiting=await waiter
        results.append(dict(lock_acquire_seconds=acquired-started,
            verify_one_seconds=first-acquired,verify_two_seconds=second-first,
            lock_held_seconds=released-acquired,contender_wait_seconds=waiting,
            release_digest=digest))
    await engine.dispose()
    print(json.dumps(results))
asyncio.run(main())
"""


@pytest.mark.integration
def test_candidate_inventory_is_protected_and_launch_does_not_migrate(unapproved_release_image) -> None:
    """Catch writable installed code and accidentally starting a second migration."""
    image, _ = unapproved_release_image
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    expected = ["/app/.venv/bin/python", "-m", "bfx_funding_bot.modules.marketfeed.daemon"]
    measured = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--read-only", image,
        "/app/.venv/bin/python", "-c",
        "from pathlib import Path; from bfx_funding_bot.core.release_identity import "
        "measure_inventory, measure_python_inventory; "
        "print(len(measure_inventory(Path('/app'), require_protected=True))); "
        "print(len(measure_python_inventory(Path('/usr/local'), require_protected=True)))",
    ], capture_output=True, text=True)
    assert measured.returncode == 0, measured.stderr
    assert inspected["Config"]["Cmd"] == expected


@pytest.mark.integration
async def test_actual_readonly_consumer_verifies_producer_receipt(
    tmp_path, pg_session_factory, pg_container, unapproved_release_image, monkeypatch,
) -> None:
    """Real consumer/PG, test command only: never start a venue-capable daemon."""
    from scripts import release_package
    # Exercise real create/inspect/publish/start and runtime proof without
    # starting marketfeed. The default daemon entrypoint is checked separately.
    monkeypatch.setattr(release_package, "LAUNCH", ["/app/.venv/bin/python", "-c",
        "from pathlib import Path; from bfx_funding_bot.core.release_identity import ReleaseRuntime; "
        "p=ReleaseRuntime(root=Path('/app'),manifest_path=Path('/run/bfx-release/manifest.json'),"
        "receipt_path=Path('/run/bfx-release/launch.json')).verify(); "
        "print('release_identity_verified:'+p.actual_image_id)"])
    from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
    from bfx_funding_bot.modules.accounts.exchange_accounts import (
        create_exchange_account_credential,
    )
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository

    account = uuid4()
    kek = b"f" * 32
    repo = CapitalRepository(account_id=account, environment="prod", max_snapshot_age_ms=300000)
    async with pg_session_factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="packaging-fixture"))
        await session.flush()
        await create_exchange_account_credential(session, exchange_account_id=account,
            venue="bitfinex", label="synthetic", api_key="fixture-not-a-real-key",
            lifecycle_status="active", verified_at=datetime.now(UTC),
            **asdict(encrypt_secret_with_aad("fixture-not-a-real-secret", aad=str(account), kek=kek)))
        for symbol in ("fUST", "fUSD"):
            await repo.apply_policy(session, symbol=symbol, expected_revision=0,
                policy=CapitalPolicy(enabled=symbol == "fUST"), source={"fixture": True})
    halt = TradingStateRepository(pg_session_factory, account_id=account, deployment_environment="prod")
    epoch = (await halt.transition("HALTED", cause="operator", reason="fixture", actor="fixture")).state
    image, identity = unapproved_release_image
    network = "task5-fixture-" + uuid4().hex
    release_dir = tmp_path.resolve() / "release"
    release_dir.mkdir()
    env_file = tmp_path / "fixture.env"
    env_file.write_text("\n".join([
        "BFX_PHASE=live", "BFX_DEPLOYMENT_ENV=prod", "BFX_EXECUTOR=bitfinex_live",
        "BFX_WS_CLIENT_ENABLED=true",
        "BFX_EXECUTION_POLICY=book_guarded", "BFX_BOOK_MAX_AGE_SECONDS=30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS=15", "BFX_BOOK_MAX_DOWN_PCT=0.15",
        "BFX_CELLS_YAML=/app/configs/cells.live.yaml",
        "BFX_SAFETY_CONFIG=/app/configs/safety.live.yaml",
        "BFX_EXCHANGE_ACCOUNT_ID=" + str(account),
        "BFX_VAULT_KEK=" + base64.b64encode(kek).decode(),
        "DATABASE_URL=postgresql://test:test@fixture-db:5432/test",
    ]))
    measurement = json.loads(run_one_shot(identity, env=env_file, network="none", tmpfs=False,
        command=["/app/.venv/bin/python", "-c", MEASURE]))
    seconds = measurement.pop("measurement_seconds")
    manifest = ReleaseManifest(version=2, release_id="unapproved-fixture",
        source_revision="a" * 40, image=identity, **measurement)

    def publish_file(name, value, uid=0, mode=0o444):
        run(["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--user", "0",
            "--mount", f"type=bind,src={release_dir},dst=/proof", image,
            "/app/.venv/bin/python", "-c",
            "import sys,os; from pathlib import Path; p=Path('/proof')/sys.argv[1]; "
            "p.write_bytes(sys.stdin.buffer.read()); p.chmod(int(sys.argv[3])); os.chown(p,int(sys.argv[2]),int(sys.argv[2]))",
            name, str(uid), str(mode)], data=value if isinstance(value, bytes) else value.model_dump_json().encode())

    container = None
    run(["docker", "network", "create", "--internal", network])
    pg_id = pg_container.get_wrapped_container().id
    run(["docker", "network", "connect", "--alias", "fixture-db", network, pg_id])
    try:
        publish_file("manifest.json", manifest)
        dr_mounts = {}
        evidence = {"exchange_account_id": str(account), "deployment_environment": "prod",
            "event_head": 0, "event_hash": "fixture", "venue_snapshot_fence": 0,
            "venue_snapshot_observed_at_ms": 0, "venue_snapshot_complete": False,
            "preflight_observed_at_ms": 0, "backup_rpo_seconds": 1, "restore_rto_seconds": 2,
            "config_digest": manifest.inventory["configs/safety.live.yaml"],
            "image_digest": image, "projector_version": "execution-state-v1",
            "migration_head": manifest.schema_head, "schema_heads": [manifest.schema_head],
            "config_artifact_path": "/app/configs/safety.live.yaml"}
        for kind, key, prefix, seconds_value in (("backup", "rpo_seconds", "backup", 1),
                                               ("restore", "rto_seconds", "isolated_restore", 2)):
            content = json.dumps({"schema_version": 1, "kind": kind, "measured": True,
                key: seconds_value, "observed_at_ms": time.time_ns() // 1000000}).encode()
            name = kind + ".json"
            publish_file(name, content, uid=1000, mode=0o600)
            dest = "/run/bfx-dr/" + name
            dr_mounts[dest] = release_dir / name
            evidence[prefix + "_evidence_path"] = dest
            evidence[prefix + "_evidence_hash"] = hashlib.sha256(content).hexdigest()
        publish_file("halt2.json", json.dumps(evidence).encode())
        mounted = ["--mount", f"type=bind,src={release_dir},dst=/run/bfx-release,readonly"]
        for dest, source in dr_mounts.items():
            mounted += ["--mount", f"type=bind,src={source},dst={dest},readonly"]
        # Validate actual runtime UID0600/readonly DR contract; this is synthetic
        # DR data, not a real backup/restore or complete Halt2 readiness claim.
        run(["docker", "run", "--rm", "--read-only", "--network", "none", *mounted, image,
            "/app/.venv/bin/python", "-c",
            "from pathlib import Path; import os; from scripts.halt2_cutover import _load_evidence; "
            "from scripts.run_canary_preflight import _require_halt2_artifacts; "
            "_require_halt2_artifacts(_load_evidence(Path('/run/bfx-release/halt2.json')),Path('/app/configs/safety.live.yaml')); "
            "assert os.getuid()==1000; assert os.statvfs('/run/bfx-dr/backup.json').f_flag & os.ST_RDONLY"])
        container = create_launch(manifest, release_dir=release_dir, env_files=[env_file],
            network=network, evidence_mounts=dr_mounts, publish=lambda receipt: publish_file("launch.json", receipt))
        started = time.monotonic()
        logs = ""
        while time.monotonic() - started < 20:
            result = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            logs = result.stdout + result.stderr
            if "release_identity_verified" in logs:
                break
            if json.loads(run(["docker", "inspect", container]))[0]["State"]["Status"] == "exited":
                break
            await asyncio.sleep(0.2)
        assert "release_identity_verified:" + image in logs, logs[-4000:]
        assert (await halt.current()).id == epoch.id
        assert (await halt.current()).state == "HALTED"
        startup_seconds = time.monotonic() - started
        # A separate read-only process measures hashes on the SAME image.
        measured = await asyncio.to_thread(subprocess.run, ["docker", "run", "--rm", "--pull=never",
            "--read-only", "--network", network, "--env-file", str(env_file),
            "--mount", f"type=bind,src={release_dir},dst=/run/bfx-release,readonly", image,
            "/app/.venv/bin/python", "-c", LOCK_MEASUREMENT], capture_output=True, text=True)
        assert measured.returncode == 0, measured.stderr
        locks = json.loads(measured.stdout)
        assert len(locks) == 3
        assert all(row["contender_wait_seconds"] > 0 for row in locks)
        assert (await halt.current()).id == epoch.id
        print(json.dumps({"fixture_image": image, "inventory_files": len(manifest.inventory),
            "python_inventory_files": len(manifest.python_inventory),
            "full_measurement_seconds": seconds,
            "consumer_startup_seconds": startup_seconds, "lock_measurements": locks}))
    finally:
        if container:
            run(["docker", "rm", "-f", container])
        run(["docker", "network", "disconnect", network, pg_id])
        run(["docker", "network", "rm", network])


@pytest.mark.integration
@pytest.mark.parametrize("exit_code", [0, 17])
def test_actual_one_shot_output_exit_status_and_cleanup(tmp_path, unapproved_release_image, exit_code):
    _, identity = unapproved_release_image
    env = tmp_path / "fixture.env"
    env.write_text("")
    created = []
    def runner(args, *, data=None):
        output = run(args, data=data)
        if args[:2] == ["docker", "create"]:
            created.append(output.decode().strip())
        return output
    command = ["/app/.venv/bin/python", "-c", f"print('fixture-output'); raise SystemExit({exit_code})"]
    try:
        if exit_code:
            with pytest.raises(PackagingBlocked, match="one_shot_exit_nonzero:17"):
                run_one_shot(identity, env=env, network="none", command=command, runner=runner)
        else:
            assert run_one_shot(identity, env=env, network="none", command=command, runner=runner) == b"fixture-output\n"
        assert len(created) == 1
        assert subprocess.run(["docker", "inspect", created[0]], capture_output=True).returncode != 0
    finally:
        # A failed cleanup assertion must not leak the exact test-owned object.
        for cid in created:
            if subprocess.run(["docker", "inspect", cid], capture_output=True).returncode == 0:
                run(["docker", "rm", "--force", "--volumes", cid])
