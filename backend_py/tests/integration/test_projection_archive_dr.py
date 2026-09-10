"""Archive DR uses disposable PG, independently captured manifests and actual bytes."""

import base64
import hashlib
import importlib
import json

import pytest
from sqlalchemy import text

from tests.integration.test_projection_cutover_archive import (
    SCOPE,
    archive_pg,  # noqa: F401
    capture,
)
from tests.integration.test_projection_cutover_archive import (
    archive_db as archive_db,
)

pytestmark = pytest.mark.integration


def _transport(expected):
    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row, encode_row
    from bfx_funding_bot.modules.execution.projection_cutover.manifest import encode_manifest
    prepared = encode_row({"kind": "projection-cutover-prepared-v1",
        "diagnostic_digest": "a" * 64, "classification_digest": "b" * 64,
        "operational_digest": "c" * 64, "runtime_roles": ["bot"], "snapshot": {},
        "manifest": decode_row(encode_manifest(expected))})
    return json.dumps({"schema_version": 1, "prepared": [{
        "sha256": hashlib.sha256(prepared).hexdigest(),
        "payload": base64.b64encode(prepared).decode(),
    }]}).encode()


@pytest.mark.parametrize("mutation", ["unchanged", "tampered-prefix", "append-full", "append-archive-only"])
async def test_original_nonempty_event_identity_is_verified(archive_db, mutation):
    factory, engine = archive_db
    insert = text("INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,event_type,payload,occurred_at_ms) VALUES (:s,:a,'ci','CREDIT_CLOSED','{}',1)")
    with engine.begin() as conn:
        conn.execute(insert, {"s": str(SCOPE.account_id), "a": SCOPE.account_id})
    expected = await capture(factory)
    with engine.begin() as conn:
        if mutation == "tampered-prefix":
            conn.exec_driver_sql("SET LOCAL session_replication_role=replica")
            conn.exec_driver_sql("UPDATE event_log SET payload='{\"tampered\":true}'")
        elif mutation.startswith("append"):
            conn.execute(insert, {"s": str(SCOPE.account_id), "a": SCOPE.account_id})
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        if mutation in {"tampered-prefix", "append-archive-only"}:
            with pytest.raises(ValueError, match="identity"):
                await module.verify_restore_archives(session, scope=SCOPE, raw=_transport(expected),
                    archive_only=mutation == "append-archive-only")
        else:
            result = await module.verify_restore_archives(session, scope=SCOPE, raw=_transport(expected),
                archive_only=mutation == "unchanged")
            assert result["event_count"] == (2 if mutation == "append-full" else 1)
            assert result["archives"][0]["original_event_count"] == 1


@pytest.mark.parametrize("legacy", [True, False])
async def test_legitimate_no_archive_scope_remains_compatible(archive_db, legacy):
    factory, engine = archive_db
    if legacy:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA projection_audit CASCADE")
            conn.exec_driver_sql("UPDATE alembic_version SET version_num='e7b1c2d3e4f5'")
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        report = await module.verify_restore_archives(session, scope=SCOPE,
            raw=b'{"schema_version":1,"prepared":[]}', archive_only=False)
    assert report["archives"] == []


async def test_inventory_is_scoped_and_rejects_omitted_second_run(archive_db):
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    factory, _ = archive_db
    first = await capture(factory)
    await capture(factory)
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        assert await module.verify_archives(session, scope=Scope(SCOPE.account_id, "prod"), expected=()) == []
        with pytest.raises(ValueError, match="inventory"):
            await module.verify_archives(session, scope=SCOPE, expected=(first,))
        with pytest.raises(ValueError, match="scope"):
            await module.verify_archives(session, scope=Scope(SCOPE.account_id, "prod"), expected=(first,))


async def test_archive_dr_preserves_sql_null_and_json_null_distinction(archive_db):
    from bfx_funding_bot.modules.execution.projection_cutover.codec import JSON_NULL, decode_row
    factory, engine = archive_db
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,event_type,payload,occurred_at_ms) VALUES (:s,:a,'ci','CREDIT_CLOSED','{}',1)"), {"s": str(SCOPE.account_id), "a": SCOPE.account_id})
        conn.execute(text("INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,symbol,kind,correlation_key,intended_amount,evidence,opened_event_seq) VALUES (:a,'ci','fUST','submit_outcome_unknown','json-null',1,'null'::jsonb,1)"), {"a": SCOPE.account_id})
    expected = await capture(factory)
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        report = await module.verify_restore_archives(session, scope=SCOPE, raw=_transport(expected), archive_only=True)
        payload = await session.scalar(text("SELECT encoded_payload FROM projection_audit.rows WHERE table_name='execution_uncertainties'"))
    assert report["archives"][0]["verified_counts"]["execution_uncertainties"] == 1
    row = decode_row(payload)
    assert row["evidence"] is JSON_NULL
    assert row["resolution_evidence"] is None


async def test_real_archive_cli_preserves_pinned_prepared_manifest(archive_db, tmp_path):
    import os
    import subprocess
    import sys
    factory, engine = archive_db
    expected = await capture(factory)
    raw = _transport(expected)
    path = tmp_path / "input.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    result = subprocess.run((sys.executable, "scripts/verify_projection_archive.py",
        "--account-id", str(SCOPE.account_id), "--environment", "ci", "--archive-only",
        "--input", str(path), "--input-digest", hashlib.sha256(raw).hexdigest()),
        env={"PATH": os.environ["PATH"], "DATABASE_URL": engine.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["archives"][0]["manifest_digest"] == expected.digest
    assert report["archive_only"] is True


async def test_archive_dr_reads_inventory_and_bytes_without_projection_parity(archive_db):
    factory, engine = archive_db
    expected = await capture(factory)
    with engine.begin() as conn:
        conn.execute(text("UPDATE position_state SET reserved=999 WHERE deployment_environment='ci'"))
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        report = await module.verify_archives(session, scope=SCOPE, expected=(expected,))
    assert report[0]["run_id"] == str(expected.run_id)
    assert report[0]["verified_counts"]["position_state"] == 1
    assert report[0]["manifest_digest"] == expected.digest


@pytest.mark.parametrize("mutation", ["payload", "row", "table", "run", "extra-table"])
async def test_archive_dr_rejects_real_tampering(archive_db, mutation):
    factory, engine = archive_db
    expected = await capture(factory)
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role=replica")
        if mutation == "payload":
            conn.execute(text("UPDATE projection_audit.rows SET encoded_payload=convert_to('{}','UTF8') WHERE table_name='position_state'"))
        elif mutation in {"row", "table"}:
            conn.execute(text("DELETE FROM projection_audit.rows WHERE table_name='position_state'"))
        elif mutation == "run":
            conn.execute(text("DELETE FROM projection_audit.runs"))
        else:
            conn.execute(text("UPDATE projection_audit.rows SET table_name='extra' WHERE table_name='position_state'"))
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        with pytest.raises(ValueError):
            await module.verify_archives(session, scope=SCOPE, expected=(expected,))


async def test_legacy_missing_expectations_rejects_completed_archive(archive_db):
    factory, _ = archive_db
    await capture(factory)
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        with pytest.raises(ValueError, match="inventory"):
            await module.verify_archives(session, scope=SCOPE, expected=())


async def test_upgraded_schema_cannot_hide_inventory_by_dropping_archive_schema(archive_db):
    factory, engine = archive_db
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA projection_audit CASCADE")
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        with pytest.raises(ValueError, match="inventory"):
            await module.verify_archives(session, scope=SCOPE, expected=())


async def test_verifier_never_autoflushes_callers_pending_state(archive_db):
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount

    factory, _ = archive_db
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        pending = ExchangeAccount(id=uuid4(), venue="bitfinex", label="never-flush")
        session.add(pending)
        await module.verify_restore_archives(session, scope=SCOPE,
            raw=b'{"schema_version":1,"prepared":[]}', archive_only=False)
        assert pending in session.new


async def test_prepare_only_verifier_proves_original_identity_and_preserves_bytes(archive_db):
    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row, encode_row
    from bfx_funding_bot.modules.execution.projection_cutover.manifest import encode_manifest

    factory, _ = archive_db
    expected = await capture(factory)
    prepared = encode_row({"kind": "projection-cutover-prepared-v1",
        "diagnostic_digest": "a" * 64, "classification_digest": "b" * 64,
        "operational_digest": "c" * 64, "runtime_roles": ["bot"], "snapshot": {},
        "manifest": decode_row(encode_manifest(expected))})
    transport = json.dumps({"schema_version": 1, "prepared": [{
        "sha256": hashlib.sha256(prepared).hexdigest(),
        "payload": base64.b64encode(prepared).decode(),
    }]}).encode()
    module = importlib.import_module("scripts.verify_projection_archive")
    async with factory() as session:
        report = await module.verify_restore_archives(session, scope=SCOPE, raw=transport, archive_only=True)
    assert report["event_count"] == 0
    assert report["event_head"] is None
    assert report["event_hash"] == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert report["archives"][0]["original_event_head"] == 0
    assert report["archives"][0]["prepared_digest"] == hashlib.sha256(prepared).hexdigest()
