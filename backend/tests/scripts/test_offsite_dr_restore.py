"""Offline contracts for the isolated offsite restore drill."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from scripts.verify_projection_replay import _TEMPORARY_TABLE_NAMES, replay_one_account
from tests.scripts.dr_measurement import read_measurement

ROOT = Path(__file__).resolve().parents[3]
COMMANDS_PATH = ROOT / "deploy/vm/pgbackrest/restore_commands.py"
DRILL_PATH = ROOT / "deploy/vm/pgbackrest/restore_drill.py"
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"
CONFIG_PATH = ROOT / "deploy/vm/pgbackrest/pgbackrest.conf"
ABSOLUTE_COMPOSE_PATH = str(ROOT / "docker-compose.dr.yml")
IMAGE_LABELS = {
    "org.bfx.postgresql.base-digest": "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
    "org.bfx.pgbackrest.version": "2.59.1",
    "org.bfx.pgbackrest.source-sha256": "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


restore_commands = _load_module("offsite_dr_restore_commands", COMMANDS_PATH)
restore_drill = _load_module("offsite_dr_restore_drill", DRILL_PATH)
evidence = _load_module("offsite_dr_evidence_for_restore", EVIDENCE_PATH)
RestoreInputError = restore_commands.RestoreInputError
build_restore_plan = restore_commands.build_restore_plan
DrillRequest = restore_drill.DrillRequest
RestoreDrill = restore_drill.RestoreDrill
EvidenceError = evidence.EvidenceError
def render_restore_evidence(**kwargs):
    return evidence.render_restore_evidence(**{
        "archive_json": _archive_report(), "verifier_image_digest": "sha256:" + "b" * 64,
        **kwargs,
    })


@pytest.fixture(autouse=True)
def clean_default_config_boundary(monkeypatch: pytest.MonkeyPatch):
    # Lifecycle unit tests do not depend on the developer's index/dirty state.
    # Separate real-Git prerequisite tests below exercise all clean-config gates.
    original = restore_drill._config_is_clean_tracked
    monkeypatch.setattr(restore_drill, "_config_is_clean_tracked",
                        lambda path: True if path == CONFIG_PATH else original(path))


@pytest.mark.integration
async def test_generated_verifier_replays_snapshots_without_source_sequence_privileges(
    pg_engine, pg_session_factory,
) -> None:
    """A copied source default must fail here, even if bootstrap grants widen."""
    from decimal import Decimal
    from uuid import UUID

    import psycopg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved

    account_id = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=account_id, venue="bitfinex", label="dr-replay"))
        await session.flush()
        for timestamp in (1_000, 2_000):
            await PostgresEventStore(deployment_environment="ci").append_snapshot(
                session,
                VenueSnapshotObserved(
                    account_id=str(account_id), environment="ci",
                    query_started_at_ms=timestamp, query_finished_at_ms=timestamp + 1,
                    offers=(), credits=(), wallet_available={"fUST": Decimal("7")},
                    coverage=SnapshotCoverage(True, True, True),
                ),
            )
        await session.commit()

    # Execute the production bootstrap SQL against the isolated testcontainer;
    # only the Docker transport is replaced, not role creation or permissions.
    admin_url = pg_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)

    def bootstrap_runner(command, *, input_text=None, timeout=None):
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(input_text)
        return subprocess.CompletedProcess(command, 0, "", "")

    async with pg_engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))

    plan = build_restore_plan(
        account_id=str(account_id), environment="ci", projector_version="execution-state-v1",
        backup_label="20260904031700-F", target_time=None,
        run_id=restore_drill._new_run_id(), database_name=pg_engine.url.database,
        expected_event_hash="a" * 64,
    )
    password = restore_drill._new_password()
    drill = RestoreDrill(command_runner=bootstrap_runner)
    drill._deadline = restore_drill.time.monotonic() + 3600
    drill._bootstrap_role(plan, password)
    verifier_engine = create_async_engine(
        pg_engine.url.set(username=plan.verify_role, password=password),
        pool_size=2, max_overflow=0,
    )
    verifier_sessions = async_sessionmaker(verifier_engine, expire_on_commit=False)

    async def source_state():
        async with pg_engine.connect() as connection:
            tables = {
                name: (await connection.execute(text(
                    f"SELECT to_jsonb(t) FROM public.{name} t ORDER BY to_jsonb(t)::text"
                ))).scalars().all()
                for name in _TEMPORARY_TABLE_NAMES
            }
            sequences = {
                name: (await connection.execute(text(
                    f"SELECT last_value, is_called FROM public.{name}"
                ))).one()
                for name in ("reconcile_observation_id_seq", "event_log_event_seq_seq")
            }
        return tables, sequences

    try:
        before = await source_state()
        async with verifier_sessions() as session:
            assert await session.scalar(text("SELECT current_user")) == plan.verify_role
            assert await session.scalar(text(
                "SELECT bool_or(has_sequence_privilege(oid, 'USAGE, UPDATE')) "
                "FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'S'"
            )) is False
            assert await session.scalar(text(
                "SELECT bool_or(has_table_privilege(oid, 'INSERT, UPDATE, DELETE, TRUNCATE')) "
                "FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
            )) is False
            first = await replay_one_account(
                session, account_id=account_id, environment="ci",
                projector_version=plan.projector_version,
            )
            # The source session holds one connection; the only other pooled
            # connection ran replay and must retain neither tables nor sequence.
            async with verifier_engine.connect() as connection:
                assert await connection.scalar(text(
                    "SELECT count(*) FROM pg_class WHERE relnamespace = pg_my_temp_schema()"
                )) == 0
            second = await replay_one_account(
                session, account_id=account_id, environment="ci", expected_event_hash=first.event_hash,
                projector_version=plan.projector_version,
            )
        assert first.row_counts["event_log"] == 2
        assert first.row_counts["reconcile_observation"] == 2
        assert first.row_counts["position_state"] == 1
        assert all(item["matches"] for item in first.diagnostic_diff.values())
        assert first.content_hashes == second.content_hashes
        assert await source_state() == before
    finally:
        await verifier_engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(
                psycopg.sql.Identifier(plan.verify_role)
            ))
            connection.execute(psycopg.sql.SQL("DROP ROLE {}").format(
                psycopg.sql.Identifier(plan.verify_role)
            ))


def _baseline_payload() -> dict[str, object]:
    return {
        "target_backup_label": "20260904031700-F", "target_time": None,
        "database_name": "bfx", "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
        "environment": "prod", "projector_version": "projector-v3",
        "migration_heads": ["head-a", "head-b"], "event_count": 9,
        "event_head": 42, "event_hash": "a" * 64,
    }


def _baseline():
    return evidence.RestoreBaseline(**(_baseline_payload() | {"migration_heads": ("head-a", "head-b")}))


def _archive_report():
    return json.dumps({"schema_version": 1,
        "scope": {"account_id": _baseline_payload()["account_id"], "environment": "prod"},
        "event_count": 9, "event_head": 42, "event_hash": "a" * 64,
        "migration_heads": ["head-a", "head-b"], "archives": [], "archive_only": False})


def _is_verifier(command):
    return any(script in command for script in (
        "scripts/verify_projection_archive.py", "scripts/verify_projection_replay.py"))


def test_legacy_drill_always_runs_archive_inventory_verifier(tmp_path):
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 0
    assert any("scripts/verify_projection_archive.py" in argv for argv in fake.commands)


def test_archive_only_requires_independent_archive_baseline(tmp_path):
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    request = replace(_request(tmp_path), archive_only=True)
    assert _drill(tmp_path, fake).run(request) == 2


def _prepared_request(tmp_path, fake, *, archive_only=True):
    request = _request(tmp_path)
    prepared = tmp_path / "prepared.json"
    prepared.write_bytes(b"opaque protected prepared artifact")
    prepared.chmod(0o600)
    ref = {"run_id": "00000000-0000-0000-0000-000000000123", "manifest_digest": "d" * 64,
           "prepared_path": str(prepared), "prepared_digest": hashlib.sha256(prepared.read_bytes()).hexdigest()}
    payload = _baseline_payload() | {"schema_version": 2, "archives": [ref],
                                   "verifier_image_digest": "sha256:" + "b" * 64}
    request.baseline_path.write_text(json.dumps(payload))
    request.baseline_path.chmod(0o600)
    report = json.loads(_archive_report())
    report["archive_only"] = archive_only
    if archive_only:
        report.update(schema_version=2, target_run_id=ref["run_id"])
    tables = list(evidence._archive.TABLES)
    report["archives"] = [{"run_id": ref["run_id"], "manifest_digest": ref["manifest_digest"],
        "scope": report["scope"], "verified_tables": tables,
        "verified_counts": dict.fromkeys(tables, 0), "verified_digests": dict.fromkeys(tables, "e" * 64),
        "original_event_count": 9, "original_event_head": 42, "original_event_hash": "a" * 64,
        "image_digest": "sha256:" + "b" * 64, "projector_version": "projector-v3",
        "migration_heads": ["head-a", "head-b"], "prepared_digest": ref["prepared_digest"]}]
    fake.archive_report = json.dumps(report)
    return replace(request, archive_only=archive_only, target_run_id=ref["run_id"] if archive_only else None)


def test_prepare_only_physically_restores_and_never_accepts_active_parity(tmp_path):
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 2, _replay_report(matches=False), ""))
    request = _prepared_request(tmp_path, fake)
    assert _drill(tmp_path, fake).run(request) == 0
    report = json.loads((tmp_path / "restore.json").read_text())
    assert report["kind"] == "archive_restore"
    assert report["restore_run_id"] == "20260904t031700z-a1b2c3d4e5f60718"
    assert len(report["archive_input_digest"]) == 64
    assert report["target_backup_label"] == "20260904031700-F"
    assert report["archive_verification"]["archives"][0]["original_event_hash"] == "a" * 64
    assert any("restore-db" in command and "up" in command for command in fake.commands)
    assert any("scripts/verify_projection_archive.py" in command for command in fake.commands)
    archive_command = next(command for command in fake.commands if "scripts/verify_projection_archive.py" in command)
    assert archive_command[archive_command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert not any("scripts/verify_projection_replay.py" in command for command in fake.commands)
    with pytest.raises(ValueError):
        read_measurement(tmp_path / "restore.json", key="rto_seconds")
    with pytest.raises(ValueError):
        read_measurement(tmp_path / "restore.json", key="rpo_seconds")


@pytest.mark.parametrize("mutation", ["valid", "request-missing", "request-unknown", "report-target",
                                     "omit-old", "old-image", "target-image", "target-projector", "target-heads"])
def test_prepare_only_target_is_bound_without_requiring_historical_current_identity(tmp_path, mutation):
    transported = []
    class TransportObservingRunner(_FakeRunner):
        def __call__(self, command, **kwargs):
            if "scripts/verify_projection_archive.py" in command:
                path = Path(command[command.index("--volume") + 1].split(":", 1)[0])
                raw = path.read_bytes()
                assert hashlib.sha256(raw).hexdigest() == command[command.index("--input-digest") + 1]
                transported.append(raw)
            return super().__call__(command, **kwargs)
    fake = TransportObservingRunner(verifier=subprocess.CompletedProcess(("fake",), 2, "", ""))
    request = _prepared_request(tmp_path, fake)
    report = json.loads(fake.archive_report)
    target = report["archives"][0]["run_id"]
    report.update(schema_version=2, target_run_id=target)
    payload = json.loads(request.baseline_path.read_text())
    old_id = "00000000-0000-0000-0000-000000000456"
    payload["archives"].append({**payload["archives"][0], "run_id": old_id})
    request.baseline_path.write_text(json.dumps(payload))
    old = {**report["archives"][0], "run_id": old_id, "original_event_count": 1,
           "original_event_head": 2, "original_event_hash": "f" * 64,
           "image_digest": "sha256:" + "c" * 64, "projector_version": "historical",
           "migration_heads": ["historical-head"]}
    report["archives"].insert(0, old)
    request = replace(request, target_run_id=target)
    if mutation == "request-missing":
        request = replace(request, target_run_id=None)
    elif mutation == "request-unknown":
        request = replace(request, target_run_id="00000000-0000-0000-0000-000000000789")
    elif mutation == "report-target":
        report["target_run_id"] = old_id
    elif mutation == "omit-old":
        report["archives"].pop(0)
    elif mutation == "old-image":
        old["image_digest"] = "invalid"
    elif mutation.startswith("target-"):
        field, value = {"target-image": ("image_digest", "sha256:" + "f" * 64),
                        "target-projector": ("projector_version", "other"),
                        "target-heads": ("migration_heads", ["other-head"])}[mutation]
        report["archives"][1][field] = value
    fake.archive_report = json.dumps(report)
    assert _drill(tmp_path, fake).run(request) == (0 if mutation == "valid" else 2)
    receipt = json.loads((tmp_path / "restore.json").read_text())
    if mutation == "valid":
        assert receipt["schema_version"] == 2
        assert receipt["target_run_id"] == target
        assert receipt["archive_verification"]["target_run_id"] == target
        assert len(receipt["archive_verification"]["archives"]) == 2
        assert json.loads(transported[0])["target_run_id"] == target
        assert receipt["archive_input_digest"] == hashlib.sha256(transported[0]).hexdigest()
    else:
        assert receipt["measured"] is False


def test_full_restore_requires_active_parity_after_archive_success(tmp_path):
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(matches=False), ""))
    request = _prepared_request(tmp_path, fake, archive_only=False)
    assert _drill(tmp_path, fake).run(request) == 2
    assert json.loads((tmp_path / "restore.json").read_text())["measured"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("original_event_count", 10), ("original_event_head", 0),
    ("migration_heads", []), ("projector_version", ""),
])
def test_full_restore_rejects_malformed_archive_identity_even_with_active_parity(tmp_path, field, value):
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    request = _prepared_request(tmp_path, fake, archive_only=False)
    report = json.loads(fake.archive_report)
    report["archives"][0][field] = value
    fake.archive_report = json.dumps(report)
    assert _drill(tmp_path, fake).run(request) == 2


@pytest.mark.parametrize("mutation", ["missing", "extra", "scope", "source", "version", "bot-image", "manifest", "cleanup"])
def test_archive_restore_fails_closed_on_binding_or_cleanup(tmp_path, mutation):
    class Runner(_FakeRunner):
        def __call__(self, command, **kwargs):
            if mutation == "cleanup" and command[:3] == ("docker", "volume", "rm"):
                return subprocess.CompletedProcess(command, 2, "", "")
            return super().__call__(command, **kwargs)
    fake = Runner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    request = _prepared_request(tmp_path, fake)
    report = json.loads(fake.archive_report)
    if mutation == "missing":
        report["archives"] = []
    elif mutation == "extra":
        report["archives"] *= 2
    elif mutation == "scope":
        report["scope"]["environment"] = "ci"
    elif mutation == "source":
        report["archives"][0]["original_event_hash"] = "f" * 64
    elif mutation == "version":
        report["schema_version"] = True
    elif mutation == "bot-image":
        report["archives"][0]["image_digest"] = "sha256:" + "f" * 64
    elif mutation == "manifest":
        report["archives"][0]["manifest_digest"] = "f" * 64
    fake.archive_report = json.dumps(report)
    assert _drill(tmp_path, fake).run(request) == 2
    output = json.loads((tmp_path / "restore.json").read_text())
    assert output["measured"] is False
    assert output["kind"] == "archive_restore"


def _replay_report(*, matches: bool = True) -> str:
    projection_names = (
        "offer_claims",
        "position_state",
        "venue_offer_state",
        "venue_credit_state",
        "projection_heads",
        "reconcile_observation",
        "submission_attempts",
        "execution_uncertainties",
    )
    row_counts: dict[str, int] = {"event_log": 9}
    content_hashes: dict[str, str] = {"event_log": "a" * 64}
    diagnostic_diff: dict[str, dict[str, int | str | bool]] = {}
    for index, name in enumerate(projection_names, start=1):
        digest = f"{index:x}" * 64
        row_counts[name] = index
        content_hashes[name] = digest
        diagnostic_diff[name] = {
            "old_count": index,
            "replayed_count": index,
            "old_hash": digest,
            "replayed_hash": digest,
            "matches": matches if name == projection_names[0] else True,
        }
    return json.dumps(
        {
            "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
            "environment": "prod",
            "projector_version": "projector-v3",
            "event_head": 42,
            "event_hash": "a" * 64,
            "row_counts": row_counts,
            "content_hashes": content_hashes,
            "diagnostic_diff": diagnostic_diff,
        }
    )


@pytest.mark.parametrize("bootstrap_status", [0, 2])
def test_bootstrap_uses_existing_baseline_db_and_stdin_only_password(
    tmp_path: Path, bootstrap_status: int, capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        bootstrap_status=bootstrap_status,
    )
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == (0 if bootstrap_status == 0 else 2)
    bootstrap = [(command, sql) for command, sql in fake.inputs if "CREATE ROLE" in sql]
    assert len(bootstrap) == 1
    command, sql = bootstrap[0]
    assert command[:5] == ("docker", "exec", "--user", "postgres", "--interactive")
    assert command[6:] == (
        "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql",
        "-U", "bfx", "-d", "bfx",
    )
    health_index = next(i for i, argv in enumerate(fake.commands) if "--format={{.State.Health.Status}}" in argv)
    assert health_index < fake.commands.index(command)
    assert 'CREATE ROLE "bfx_dr_20260904t031700z_a1b2c3d4e5f60718"' in sql
    assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS" in sql
    assert "DATABASE-PASSWORD-SENTINEL" in sql
    assert 'GRANT CONNECT, TEMPORARY ON DATABASE "bfx"' in sql
    assert "GRANT USAGE ON SCHEMA public" in sql
    for table in (
        "event_log", "offer_claims", "position_state", "venue_offer_state", "venue_credit_state",
        "projection_heads", "reconcile_observation", "submission_attempts", "execution_uncertainties", "alembic_version",
    ):
        assert f'public."{table}"' in sql
    assert "CREATE DATABASE" not in sql
    assert "GRANT ALL" not in sql
    assert "ON ALL TABLES" not in sql
    assert "log_statement = 'none'" in sql
    assert "log_min_error_statement = 'panic'" in sql
    assert "log_min_duration_statement = -1" in sql
    assert fake.env_mode == 0o600
    assert "postgresql+asyncpg://bfx_dr_20260904t031700z_a1b2c3d4e5f60718:DATABASE-PASSWORD-SENTINEL@bfx-dr-20260904t031700z-a1b2c3d4e5f60718-db:5432/bfx" in fake.env_text
    assert "DATABASE-PASSWORD-SENTINEL" not in repr(fake.commands)
    report = (tmp_path / "restore.json").read_text()
    assert "DATABASE-PASSWORD-SENTINEL" not in report
    captured = capsys.readouterr()
    assert "DATABASE-PASSWORD-SENTINEL" not in captured.out + captured.err
    if bootstrap_status:
        assert not any(_is_verifier(argv) for argv in fake.commands)
        assert "DATABASE-PASSWORD-SENTINEL" not in (tmp_path / "restore.log").read_text()
        assert json.loads(report)["measured"] is False
    else:
        verifier = next(argv for argv in fake.commands if "scripts/verify_projection_replay.py" in argv)
        assert verifier[verifier.index("--expected-event-hash") + 1] == "a" * 64
        schema_command, query = next((argv, data) for argv, data in fake.inputs if "server_version_num" in data)
        assert schema_command[:5] == ("docker", "exec", "--user", "postgres", "--interactive")
        assert schema_command[schema_command.index("-d") + 1] == "bfx"
        assert "3f19d046-5030-494c-9a0a-9573bb890c1f" in query
        assert json.loads(report)["egress_disconnected"] is True


def test_missing_baseline_fails_before_resource_creation(tmp_path: Path) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    request = _request(tmp_path)
    request.baseline_path.unlink()
    assert _drill(tmp_path, fake).run(request) == 2
    assert fake.commands == []


@pytest.mark.parametrize(("field", "value"), [
    ("database_name", "bfx;DROP"), ("database_name", "x" * 64),
    ("expected_event_hash", "A" * 64), ("expected_event_hash", "a" * 63),
])
def test_expected_baseline_plan_inputs_are_validated(field: str, value: str) -> None:
    kwargs = {
        "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f", "environment": "prod",
        "projector_version": "v3", "backup_label": "20260904031700-F", "target_time": None,
        "run_id": "20260904T031700Z-a1b2c3d4e5f60718", "database_name": "bfx", "expected_event_hash": "a" * 64,
    }
    with pytest.raises(RestoreInputError):
        build_restore_plan(**(kwargs | {field: value}))


def _write_valid_secret_dir(tmp_path: Path) -> Path:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    secret_file = secret_dir / "r2.conf"
    values = {
        "repo1-s3-endpoint": "https://account.r2.cloudflarestorage.com",
        "repo1-s3-bucket": "offsite-dr",
        "repo1-s3-key": "opaque-access-key",
        "repo1-s3-key-secret": "TOKEN-SENTINEL",
        "repo1-cipher-pass": "opaque-cipher-pass",
    }
    assignments = "\n".join(f"{key}={value}" for key, value in values.items())
    secret_file.write_text(f"[global]\n{assignments}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    secret_dir.chmod(0o700)
    return secret_dir


def test_dr_compose_uses_generated_external_resources_without_production_inputs() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.dr.yml").read_text())

    assert set(compose["services"]) == {"restore-db", "verifier"}
    assert compose["volumes"] == {
        "restore-data": {"external": True, "name": "${DR_VOLUME_NAME}"}
    }
    assert compose["networks"] == {
        "dr": {"external": True, "name": "${DR_NETWORK_NAME}"},
        "r2-egress": {
            "external": True,
            "name": "${DR_EGRESS_NETWORK_NAME}",
        },
    }
    restore_db = compose["services"]["restore-db"]
    verifier = compose["services"]["verifier"]
    assert restore_db["networks"] == ["dr", "r2-egress"]
    assert verifier["networks"] == ["dr"]
    assert "restore-data:/var/lib/postgresql" in restore_db["volumes"]
    assert restore_db["healthcheck"]["test"] == [
        "CMD", "pg_isready", "-U", "${DR_SQL_ADMIN_ROLE}", "-d", "${DR_DATABASE_NAME}",
    ]
    assert "ports" not in compose["services"]["restore-db"]
    assert "ports" not in compose["services"]["verifier"]
    source = (ROOT / "docker-compose.dr.yml").read_text()
    for forbidden in (
        "POSTGRES_",
        "bot.env",
        "webapi.env",
        "frontend.env",
        ".env.runtime",
        "BFX_VAULT_KEK",
    ):
        assert forbidden not in source


def test_restore_plan_names_are_random_prefixed_and_cleanup_is_generated_only() -> None:
    plan = build_restore_plan(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
        run_id="20260904T031700Z-a1b2c3d4e5f60718",
        database_name="bfx",
        expected_event_hash="a" * 64,
    )

    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.volume_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.network_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.egress_network_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.container_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.project_name)
    assert plan.verify_role == "bfx_dr_20260904t031700z_a1b2c3d4e5f60718"
    assert plan.create_commands == (
        ("docker", "network", "create", "--internal", plan.network_name),
        ("docker", "network", "create", plan.egress_network_name),
        ("docker", "volume", "create", plan.volume_name),
    )
    assert sum(command.count("--internal") for command in plan.create_commands) == 1
    assert plan.run_commands[1] == (
        "docker",
        "network",
        "inspect",
        "--format={{.Internal}}",
        plan.egress_network_name,
    )
    assert plan.run_commands[2] == (
        "docker",
        "network",
        "disconnect",
        plan.egress_network_name,
        plan.container_name,
    )
    assert plan.run_commands[3] == (
        "docker",
        "inspect",
        "--format={{json .NetworkSettings.Networks}}",
        plan.container_name,
    )
    verifier_run_index = plan.run_commands[4].index("run")
    assert plan.run_commands[4][verifier_run_index : verifier_run_index + 7] == (
        "run",
        "--rm",
        "--no-deps",
        "--name",
        plan.verifier_container_name,
        "verifier",
        "replay",
    )
    assert plan.cleanup_commands[1:4] == (
        ("docker", "volume", "rm", plan.volume_name),
        ("docker", "network", "rm", plan.egress_network_name),
        ("docker", "network", "rm", plan.network_name),
    )
    assert all(isinstance(command, tuple) for command in (
        *plan.create_commands,
        *plan.run_commands,
        *plan.cleanup_commands,
    ))
    assert "bfx_pgdata" not in repr(plan)
    assert all(
        argument.startswith("bfx-dr-")
        for command in plan.cleanup_commands
        for argument in command
        if argument.startswith("bfx-")
    )


def test_restore_plan_rejects_command_injection_and_noncanonical_account() -> None:
    with pytest.raises(RestoreInputError):
        build_restore_plan(
            account_id="not-a-uuid",
            environment="prod",
            projector_version="v3",
            backup_label="20260904031700-F;rm",
            target_time=None,
            run_id="20260904T031700Z-a1b2c3d4e5f60718",
            database_name="bfx",
            expected_event_hash="a" * 64,
        )

    with pytest.raises(RestoreInputError):
        build_restore_plan(
            account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
            environment="prod",
            projector_version="v3",
            backup_label="20260904031700-F;rm",
            target_time=None,
            run_id="20260904T031700Z-a1b2c3d4e5f60718",
            database_name="bfx",
            expected_event_hash="a" * 64,
        )


def test_restore_plan_uses_an_absolute_compose_path_in_every_compose_argv() -> None:
    plan = build_restore_plan(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
        run_id="20260904T031700Z-a1b2c3d4e5f60718",
        database_name="bfx",
        expected_event_hash="a" * 64,
    )

    compose_commands = [
        command
        for command in (*plan.run_commands, *plan.cleanup_commands)
        if command[:2] == ("docker", "compose")
    ]
    assert compose_commands
    assert all(ABSOLUTE_COMPOSE_PATH in command for command in compose_commands)


def test_restore_evidence_contains_only_validated_measurements(tmp_path: Path) -> None:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    report = render_restore_evidence(
        schema_tsv="180000\thead-a,head-b\t9",
        replay_json=_replay_report(),
        baseline=_baseline(),
        observed_at_ms=1756961300000,
        now_ms=1756961300000,
        image_labels=IMAGE_LABELS,
        egress_disconnected=True,
        elapsed_seconds=37,
        config_path=config,
        image_digest=f"sha256:{'b' * 64}",
        network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
        network_internal=True,
    )

    assert report["measured"] is True
    assert report["rto_seconds"] == 37
    assert report["event_hash"] == "a" * 64
    assert report["row_counts"]["offer_claims"] == 1
    assert report["projection_hashes"]["offer_claims"] == "1" * 64
    assert report["network_internal"] is True
    assert report["config_digest"]
    assert report["image_digest"] == f"sha256:{'b' * 64}"


def test_restore_evidence_rejects_a_false_projection_diagnostic(tmp_path: Path) -> None:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match=r"^projection_replay_mismatch$"):
        render_restore_evidence(
            schema_tsv="180000\thead-a\t9",
            replay_json=_replay_report(matches=False),
            baseline=_baseline(),
            observed_at_ms=1756961300000,
            now_ms=1756961300000,
            image_labels=IMAGE_LABELS,
            egress_disconnected=True,
            elapsed_seconds=37,
            config_path=config,
            image_digest=f"sha256:{'b' * 64}",
            network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
            network_internal=True,
        )


class _FakeRunner:
    def __init__(
        self,
        *,
        verifier: subprocess.CompletedProcess[str],
        network_internal: str = "true\n",
        egress_internal: str = "false\n",
        container_networks: str | None = None,
        compose_up_status: int = 0,
        bootstrap_status: int = 0,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[tuple[tuple[str, ...], str]] = []
        self.env_text = ""
        self.env_mode: int | None = None
        self.verifier = verifier
        self.network_internal = network_internal
        self.egress_internal = egress_internal
        self.container_networks = container_networks
        self.compose_up_status = compose_up_status
        self.bootstrap_status = bootstrap_status
        self.timeouts: list[float | None] = []
        self.image_id = f"sha256:{'b' * 64}\n"
        self.image_labels = json.dumps(IMAGE_LABELS)
        self.archive_report = _archive_report()

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if "scripts/verify_projection_archive.py" in command:
            return subprocess.CompletedProcess(command, 0, self.archive_report, "")
        if "scripts/verify_projection_replay.py" in command:
            return self.verifier
        if command[:4] == ("docker", "image", "inspect", "--format={{.Id}}"):
            return subprocess.CompletedProcess(command, 0, self.image_id, "")
        if input_text is not None:
            self.inputs.append((command, input_text))
        if "--env-file" in command:
            env_path = Path(command[command.index("--env-file") + 1])
            self.env_text = env_path.read_text(encoding="utf-8")
            self.env_mode = env_path.stat().st_mode & 0o777
        if command[:3] == ("docker", "network", "inspect"):
            output = self.egress_internal if command[-1].endswith("-egress") else self.network_internal
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:3] == (
            "docker",
            "inspect",
            "--format={{json .NetworkSettings.Networks}}",
        ):
            networks = self.container_networks
            if networks is None:
                networks = json.dumps({"bfx-dr-20260904t031700z-a1b2c3d4e5f60718-net": {}})
            return subprocess.CompletedProcess(command, 0, f"{networks}\n", "")
        if "up" in command and "restore-db" in command:
            return subprocess.CompletedProcess(command, self.compose_up_status, "", "")
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            return subprocess.CompletedProcess(command, 0, "healthy\n", "")
        if input_text and "pg_is_in_recovery()" in input_text:
            return subprocess.CompletedProcess(command, 0, "f\n", "")
        if command[:3] == ("docker", "exec", command[2]):
            if input_text and "CREATE ROLE" in input_text:
                return subprocess.CompletedProcess(command, self.bootstrap_status, "", "DATABASE-PASSWORD-SENTINEL")
            return subprocess.CompletedProcess(command, 0, "180000\thead-a,head-b\t9\n", "")
        if "run" in command and "verifier" in command:
            return self.verifier
        if command[:4] == ("docker", "image", "inspect", "--format={{index .RepoDigests 0}}"):
            return subprocess.CompletedProcess(command, 0, f"sha256:{'b' * 64}\n", "")
        if command[:3] == ("docker", "inspect", "--format={{.Image}}"):
            return subprocess.CompletedProcess(command, 0, self.image_id, "")
        if command[:4] == ("docker", "image", "inspect", "--format={{json .Config.Labels}}"):
            return subprocess.CompletedProcess(command, 0, self.image_labels, "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _drill(
    tmp_path: Path,
    fake: _FakeRunner,
    *,
    run_id: str = "20260904T031700Z-a1b2c3d4e5f60718",
    clock: Callable[[], float] = restore_drill.time.monotonic,
) -> RestoreDrill:
    secret_dir = _write_valid_secret_dir(tmp_path)
    return RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: run_id,
        password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
        clock=clock,
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )


def _request(tmp_path: Path) -> DrillRequest:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(_baseline_payload()))
    return DrillRequest(
        baseline_path=path,
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
    )


def _timing_report(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "restore-timing.json").read_text(encoding="utf-8"))


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_sql_admin_matches_deployed_cluster() -> None:
    import configparser

    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    production = yaml.safe_load((ROOT / "docker-compose.bot.yml").read_text())
    assert production["services"]["postgres"]["env_file"] == ".env.runtime"
    assert config["bfx"].get("pg1-user") == "bfx"


@pytest.mark.parametrize("recovery", ["t", "invalid", "", "f\nt", "timeout", "poll"])
def test_recovery_must_finish_before_disconnect_and_bootstrap(tmp_path: Path, recovery: str) -> None:
    clock = _Clock()
    queries = []
    class Recovery(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if input_text and "pg_is_in_recovery()" in input_text:
                queries.append(len(self.commands) - 1)
                assert command[command.index("-U") + 1] == "bfx"
                assert command[command.index("-d") + 1] == "bfx"
                if recovery == "timeout":
                    clock.now += timeout
                    raise subprocess.TimeoutExpired(command, timeout)
                value = ("t" if len(queries) == 1 else "f") if recovery == "poll" else recovery
                return subprocess.CompletedProcess(command, 0, value + "\n", "")
            return result
    fake = Recovery(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    drill = _drill(tmp_path, fake, clock=clock)
    drill._sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    assert drill.run(_request(tmp_path)) == (0 if recovery == "poll" else 2)
    assert queries
    disconnects = [i for i, cmd in enumerate(fake.commands) if "disconnect" in cmd]
    if recovery == "poll":
        assert len(queries) == 2
        assert max(queries) < disconnects[0]
        assert next(i for i, cmd in enumerate(fake.commands) if _is_verifier(cmd)) > disconnects[0]
    else:
        assert not disconnects
        assert not any("CREATE ROLE" in data for _, data in fake.inputs)
        assert not any(_is_verifier(cmd) for cmd in fake.commands)
        assert json.loads((tmp_path / "restore.json").read_text())["measured"] is False


@pytest.mark.parametrize("networks", [{}, {"production": {}},
    {"bfx-dr-20260904t031700z-a1b2c3d4e5f60718-net": {}, "production": {}}])
def test_membership_requires_exact_generated_internal_network(tmp_path: Path, networks: dict) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
                       container_networks=json.dumps(networks))
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    assert not any(_is_verifier(cmd) for cmd in fake.commands)


def test_compose_uses_sanitized_process_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    injected = dict.fromkeys(("DR_NETWORK_NAME", "DR_CONTAINER_NAME", "DR_SQL_ADMIN_ROLE", "DR_DATABASE_NAME", "DATABASE_URL", "POSTGRES_USER", "BFX_DEPLOYMENT_ENV", "BFX_VAULT_KEK", "COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES", "COMPOSE_PROJECT_NAME"), "PRODUCTION-SENTINEL")
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DOCKER_HOST", "unix:///test-docker.sock")
    observed = []
    class Environment(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            if command[:2] == ("docker", "compose"):
                observed.append(env)
            return super().__call__(command, timeout=timeout, input_text=input_text, env=env)
    fake = Environment(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 0
    assert len(observed) == 2  # compose up and cleanup; verifiers use pinned docker run
    for environment in observed:
        assert environment is not None
        assert not set(injected) & environment.keys()
        assert environment["PATH"] == os.environ["PATH"]
        assert environment["DOCKER_HOST"] == "unix:///test-docker.sock"
    assert "PRODUCTION-SENTINEL" not in fake.env_text


def test_real_command_boundary_passes_sanitized_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    def subprocess_run(command, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(restore_drill.subprocess, "run", subprocess_run)
    environment = {"PATH": "/bin", "DOCKER_HOST": "unix:///test.sock"}
    restore_drill._run_command(("docker", "compose", "version"), timeout=1, env=environment)
    assert seen[0]["env"] == environment
    assert not seen[0].get("shell", False)


@pytest.mark.parametrize("outcome", ["timeout", "already-removed", "cleanup-failure"])
def test_verifier_container_cleanup_is_independent_of_compose_client(tmp_path: Path, outcome: str) -> None:
    clock = _Clock()
    running = set()
    attempted = []
    class Verifier(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if _is_verifier(command):
                name = command[command.index("--name") + 1] if "--name" in command else "anonymous-verifier"
                attempted.append(name)
                if outcome == "already-removed":
                    return result
                running.add(name)
                clock.now += timeout
                raise subprocess.TimeoutExpired(command, timeout)
            if command[:3] == ("docker", "container", "rm"):
                assert 0 < timeout <= 30
                if outcome in {"cleanup-failure", "already-removed"}:
                    return subprocess.CompletedProcess(command, 2, "", "TOKEN-SENTINEL")
                running.discard(command[-1])
            if command[:3] == ("docker", "container", "ls"):
                return subprocess.CompletedProcess(command, 0, "\n".join(running), "")
            return result
    fake = Verifier(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    status = _drill(tmp_path, fake, clock=clock).run(_request(tmp_path))
    assert status == (0 if outcome == "already-removed" else 2)
    assert attempted and re.fullmatch(r"bfx-dr-[a-z0-9-]+-verifier", attempted[0])
    removal = next(i for i, cmd in enumerate(fake.commands) if cmd[:3] == ("docker", "container", "rm"))
    assert fake.commands[removal][-1] == attempted[0]
    assert removal < next(i for i, cmd in enumerate(fake.commands) if cmd[:3] == ("docker", "network", "rm"))
    if outcome != "cleanup-failure":
        assert not running
    else:
        assert json.loads((tmp_path / "restore.json").read_text())["error_code"] == "cleanup_failed"


def test_image_evidence_uses_container_id_and_fixed_labels(tmp_path: Path) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 0
    report = json.loads((tmp_path / "restore.json").read_text())
    assert report["image_labels"] == IMAGE_LABELS
    assert report["image_digest"] == f"sha256:{'b' * 64}"
    assert any(command[:3] == ("docker", "inspect", "--format={{.Image}}") for command in fake.commands)
    assert ("docker", "image", "inspect", "--format={{json .Config.Labels}}", f"sha256:{'b' * 64}") in fake.commands
    assert not any("RepoDigests" in arg for command in fake.commands for arg in command)


@pytest.mark.parametrize("label", list(IMAGE_LABELS))
@pytest.mark.parametrize("value", [None, "", "TOKEN-SENTINEL", 2591, False])
def test_image_labels_fail_closed(tmp_path: Path, label: str, value: object) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    labels = dict(IMAGE_LABELS)
    if value is None:
        del labels[label]
    else:
        labels[label] = value
    fake.image_labels = json.dumps(labels)
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    report = (tmp_path / "restore.json").read_text()
    assert json.loads(report)["error_code"] == "restore_output_invalid"
    assert "TOKEN-SENTINEL" not in report + (tmp_path / "restore.log").read_text()


@pytest.mark.parametrize("field,value", [
    ("image_id", "b" * 64), ("image_id", f"repo@sha256:{'b' * 64}"),
    ("image_id", f"sha256:{'B' * 64}"), ("image_id", f"sha256:{'b' * 64}\nTOKEN-SENTINEL"),
    ("image_labels", "null"), ("image_labels", "[]"), ("image_labels", "TOKEN-SENTINEL"),
])
def test_image_metadata_malformed_fails_closed(tmp_path: Path, field: str, value: str) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    setattr(fake, field, value)
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    assert json.loads((tmp_path / "restore.json").read_text())["measured"] is False


def test_deadline_is_shared_from_first_create_and_all_commands_are_bounded(tmp_path: Path) -> None:
    clock = _Clock()

    class Advancing(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if "rm" not in command:
                clock.now += 10
            return result

    fake = Advancing(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 0
    restore_count = next(i for i, cmd in enumerate(fake.commands) if "rm" in cmd)
    for index, (command, timeout) in enumerate(zip(fake.commands, fake.timeouts, strict=True)):
        assert isinstance(command, tuple)
        assert timeout is not None and timeout > 0
        if index < restore_count:
            assert timeout <= 3600 - index * 10
            if "--format={{.State.Health.Status}}" in command:
                assert timeout <= 600
        else:
            assert timeout <= 30
    assert fake.timeouts[0] == 3600
    assert json.loads((tmp_path / "restore.json").read_text())["rto_seconds"] == restore_count * 10


def test_success_writes_separate_allowlisted_stage_timing_diagnostic(tmp_path: Path) -> None:
    clock = _Clock()

    class Advancing(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            clock.now += 1
            return result

    fake = Advancing(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))

    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 0

    accepted = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    diagnostic = _timing_report(tmp_path)
    assert accepted["rto_seconds"] == next(
        index for index, command in enumerate(fake.commands) if "rm" in command
    )
    assert "stages" not in accepted
    assert diagnostic == {
        "complete": True,
        "kind": "restore_timing",
        "restore_run_id": "20260904t031700z-a1b2c3d4e5f60718",
        "schema_version": 1,
        "stages": {
            "cleanup": 5.0,
            "isolation_bootstrap": 5.0,
            "physical_and_wal_recovery": 3.0,
            "resource_setup": 5.0,
            "verification": 4.0,
        },
    }


def test_failed_restore_times_cleanup_with_independent_bound(tmp_path: Path) -> None:
    clock = _Clock()
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        compose_up_status=2,
    )

    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2

    cleanup = [
        timeout
        for command, timeout in zip(fake.commands, fake.timeouts, strict=True)
        if "rm" in command
    ]
    accepted = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    diagnostic = _timing_report(tmp_path)
    assert cleanup and all(timeout is not None and 0 < timeout <= 30 for timeout in cleanup)
    assert accepted["measured"] is False
    assert diagnostic["complete"] is False
    assert diagnostic["restore_run_id"] == "20260904t031700z-a1b2c3d4e5f60718"
    assert set(diagnostic["stages"]) == {
        "resource_setup",
        "physical_and_wal_recovery",
        "cleanup",
    }


@pytest.mark.parametrize(
    ("compose_up_status", "expected_status", "expected_measured"),
    [(0, 0, True), (2, 2, False)],
)
def test_timing_persistence_failure_does_not_skip_cleanup_or_change_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose_up_status: int,
    expected_status: int,
    expected_measured: bool,
) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        compose_up_status=compose_up_status,
    )

    def fail_timing_write(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")

    monkeypatch.setattr(restore_drill, "_write_timing_json", fail_timing_write)

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == expected_status

    accepted = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    cleanup = [
        timeout
        for command, timeout in zip(fake.commands, fake.timeouts, strict=True)
        if "rm" in command
    ]
    assert cleanup and all(timeout is not None and 0 < timeout <= 30 for timeout in cleanup)
    assert accepted["measured"] is expected_measured
    assert set(accepted).isdisjoint({"complete", "stages"})
    assert not (tmp_path / "restore-timing.json").exists()


def test_timing_invalidation_interruption_cannot_leave_previous_green_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "restore.json"
    output.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")

    def interrupt_timing_invalidation(path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        restore_drill, "_invalidate_timing_json", interrupt_timing_invalidation
    )

    with pytest.raises(KeyboardInterrupt):
        _drill(
            tmp_path,
            _FakeRunner(
                verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
            ),
        ).run(_request(tmp_path))

    assert not output.exists()


@pytest.mark.parametrize("stage", ["create", "compose", "health", "bootstrap", "schema", "disconnect", "image", "labels", "verifier"])
def test_deadline_interrupts_blocked_commands_and_still_cleans(tmp_path: Path, stage: str) -> None:
    clock = _Clock()
    blocked = []

    class Blocking(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            match = {
                "create": command[:3] == ("docker", "network", "create"),
                "compose": "up" in command,
                "health": "--format={{.State.Health.Status}}" in command,
                "bootstrap": input_text and "CREATE ROLE" in input_text,
                "schema": input_text and "server_version_num" in input_text,
                "disconnect": "disconnect" in command,
                "image": "--format={{.Image}}" in command,
                "labels": "--format={{json .Config.Labels}}" in command,
                "verifier": _is_verifier(command),
            }[stage]
            if match and not blocked:
                blocked.append(timeout)
                clock.now += timeout if timeout is not None else 4000
                raise subprocess.TimeoutExpired(command, timeout or 4000, stderr="TOKEN-SENTINEL")
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if "rm" not in command:
                clock.now += 1
            return result

    fake = Blocking(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    assert blocked and blocked[0] is not None and 0 < blocked[0] <= 3600
    assert clock.now <= 3700
    cleanup = [(cmd, timeout) for cmd, timeout in zip(fake.commands, fake.timeouts, strict=True) if "rm" in cmd]
    assert bool(cleanup) is (stage != "create")
    assert all(0 < timeout <= 30 for _, timeout in cleanup)
    for cmd in fake.commands:
        if "--env-file" in cmd:
            assert not Path(cmd[cmd.index("--env-file") + 1]).exists()
    report = (tmp_path / "restore.json").read_text()
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report


def test_cleanup_deadline_reserves_time_to_unlink_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    unlinked_at = []
    original_unlink = restore_drill._unlink_env_file

    def unlink(path, *, timeout):
        unlinked_at.append(clock.now)
        assert 0 < timeout <= 130 - clock.now
        return original_unlink(path, timeout=timeout)

    monkeypatch.setattr(restore_drill, "_unlink_env_file", unlink)

    class CleanupBlocking(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if "rm" in command:
                clock.now += timeout if timeout is not None else 100
                raise subprocess.TimeoutExpired(command, timeout or 100)
            return result

    fake = CleanupBlocking(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    assert unlinked_at and 100 <= unlinked_at[0] < 130
    assert clock.now <= 130
    report = json.loads((tmp_path / "restore.json").read_text())
    assert report["error_code"] == "cleanup_failed"
    assert report["measured"] is False


def test_cleanup_deadline_rejects_late_success(tmp_path: Path) -> None:
    clock = _Clock()

    class LateCleanup(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if command[:3] == ("docker", "network", "rm") and command[-1].endswith("-net"):
                clock.now += 31
            return result

    fake = LateCleanup(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    report = json.loads((tmp_path / "restore.json").read_text())
    assert report["measured"] is False
    assert report["error_code"] == "cleanup_failed"


def test_cleanup_unlink_failure_replaces_green_without_leaking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_unlink = Path.unlink
    paths = []

    def failing_unlink(path, *, timeout):
        paths.append(path)
        raise OSError("TOKEN-SENTINEL")

    monkeypatch.setattr(restore_drill, "_unlink_env_file", failing_unlink)
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    try:
        assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
        report = (tmp_path / "restore.json").read_text()
        assert json.loads(report)["error_code"] == "cleanup_failed"
        assert json.loads(report)["measured"] is False
        assert "TOKEN-SENTINEL" not in report + (tmp_path / "restore.log").read_text()
    finally:
        for path in paths:
            real_unlink(path, missing_ok=True)


@pytest.mark.parametrize("create_index", [0, 1, 2])
def test_cleanup_only_removes_successfully_created_resources(tmp_path: Path, create_index: int) -> None:
    class FailedCreate(_FakeRunner):
        creates = 0

        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if command[:2] in (("docker", "network"), ("docker", "volume")) and "create" in command:
                self.creates += 1
                if self.creates == create_index + 1:
                    return subprocess.CompletedProcess(command, 2, "", "TOKEN-SENTINEL")
            return result

    fake = FailedCreate(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    cleanup = [cmd for cmd in fake.commands if "rm" in cmd]
    assert len(cleanup) == create_index
    assert [cmd[-1].rsplit("-", 1)[-1] for cmd in cleanup] == [[], ["net"], ["egress", "net"]][create_index]


def test_health_deadline_cannot_extend_nearly_expired_global_budget(tmp_path: Path) -> None:
    clock = _Clock()
    health_timeouts = []

    class SlowCompose(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if "up" in command:
                clock.now += 3590
            if "--format={{.State.Health.Status}}" in command:
                health_timeouts.append(timeout)
                clock.now += timeout if timeout is not None else 600
                raise subprocess.TimeoutExpired(command, timeout or 600)
            return result

    fake = SlowCompose(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    assert health_timeouts == [10]
    assert json.loads((tmp_path / "restore.json").read_text())["error_code"] == "restore_command_failed"


@pytest.mark.parametrize("stage", ["renderer", "persist"])
def test_deadline_rejects_late_evidence_and_cleans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    clock = _Clock()
    original = restore_drill.render_restore_evidence if stage == "renderer" else restore_drill._write_json

    def advance(*args, **kwargs):
        result = original(*args, **kwargs)
        if stage == "renderer" or args[1].get("measured") is True:
            clock.now += 3601
        return result

    monkeypatch.setattr(restore_drill, "render_restore_evidence" if stage == "renderer" else "_write_json", advance)
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    assert json.loads((tmp_path / "restore.json").read_text())["measured"] is False
    assert len([cmd for cmd in fake.commands if "rm" in cmd]) == 5


@pytest.mark.parametrize("target_time", [None, "2026-09-04T04:00:00Z"])
def test_freshness_timestamp_is_after_baseline_validation_and_target_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_time: str | None) -> None:
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    request = replace(_request(tmp_path), target_time=target_time)
    request.baseline_path.write_text(json.dumps(_baseline_payload() | {"target_time": target_time}))
    validated = []
    real_parse = restore_drill._evidence._parse_replay

    def parse(*args, **kwargs):
        result = real_parse(*args, **kwargs)
        validated.append(True)
        return result

    def time_ns():
        assert validated, "success timestamp captured before verifier/baseline validation"
        return 1756961300000 * 1_000_000

    monkeypatch.setattr(restore_drill._evidence, "_parse_replay", parse)
    monkeypatch.setattr(restore_drill.time, "time_ns", time_ns)
    assert _drill(tmp_path, fake).run(request) == 0
    report = json.loads((tmp_path / "restore.json").read_text())
    assert report["target_time"] == target_time
    assert report["observed_at_ms"] == 1756961300000
    assert report["egress_disconnected"] is True


class _ImageAndEvidenceAdvancingRunner(_FakeRunner):
    def __init__(self, clock: _Clock) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.clock = clock

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if command[:3] == ("docker", "image", "inspect"):
            self.clock.now += 1.25
        return super().__call__(command, timeout=timeout, input_text=input_text, env=env)


def test_rto_includes_image_and_evidence_validation_before_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    fake = _ImageAndEvidenceAdvancingRunner(clock)
    drill = _drill(tmp_path, fake, clock=clock)
    real_renderer = restore_drill.render_restore_evidence
    render_calls = 0

    def advancing_renderer(**kwargs: object) -> dict[str, object]:
        nonlocal render_calls
        if render_calls == 0:
            clock.now += 1.25
        render_calls += 1
        return real_renderer(**kwargs)

    monkeypatch.setattr(restore_drill, "render_restore_evidence", advancing_renderer)

    assert drill.run(_request(tmp_path)) == 0

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is True
    assert report["rto_seconds"] == 4  # bot image, PG image labels, final evidence


class _CleanupObservingRunner(_FakeRunner):
    def __init__(self, output_path: Path, *, cleanup_status: int = 0) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.output_path = output_path
        self.cleanup_status = cleanup_status
        self.evidence_present_during_cleanup: list[bool] = []

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if "rm" in command:
            self.evidence_present_during_cleanup.append(self.output_path.exists())
            if self.cleanup_status:
                return subprocess.CompletedProcess(command, self.cleanup_status, "", "")
        return super().__call__(command, timeout=timeout, input_text=input_text, env=env)


def test_cleanup_failure_never_publishes_green_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "restore.json"
    output_path.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")
    fake = _CleanupObservingRunner(output_path, cleanup_status=2)
    secret_dir = _write_valid_secret_dir(tmp_path)
    drill = RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=output_path,
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )
    replacements = []
    real_replace = os.replace

    def observe_replace(source, destination):
        replacements.append(json.loads(Path(source).read_text()))
        real_replace(source, destination)

    monkeypatch.setattr(restore_drill.os, "replace", observe_replace)

    assert drill.run(_request(tmp_path)) == 2

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert fake.evidence_present_during_cleanup
    assert all(present is False for present in fake.evidence_present_during_cleanup)
    accepted_replacements = [item for item in replacements if item["kind"] != "restore_timing"]
    assert [item["measured"] for item in accepted_replacements] == [False]
    assert report["measured"] is False
    assert report["error_code"] == "cleanup_failed"
    assert set(report) == {"schema_version", "measured", "kind", "observed_at_ms", "error_code"}
    assert (tmp_path / "restore.log").read_text() == "restore drill failed: cleanup_failed\n"


def test_success_publishes_only_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "restore.json"
    fake = _CleanupObservingRunner(output_path)
    drill = _drill(tmp_path, fake)
    replacements = []
    real_replace = os.replace

    def observe_replace(source, destination):
        replacements.append(json.loads(Path(source).read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr(restore_drill.os, "replace", observe_replace)

    assert drill.run(_request(tmp_path)) == 0

    assert fake.evidence_present_during_cleanup
    assert all(present is False for present in fake.evidence_present_during_cleanup)
    accepted_replacements = [item for item in replacements if item["kind"] != "restore_timing"]
    assert [item["measured"] for item in accepted_replacements] == [True]
    assert json.loads(output_path.read_text(encoding="utf-8"))["measured"] is True


def test_cleanup_interruption_leaves_previous_evidence_unavailable(tmp_path: Path) -> None:
    output = tmp_path / "restore.json"
    output.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")

    class InterruptingCleanup(_CleanupObservingRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            if "rm" in command:
                self.evidence_present_during_cleanup.append(self.output_path.exists())
                raise KeyboardInterrupt
            return super().__call__(command, timeout=timeout, input_text=input_text, env=env)

    fake = InterruptingCleanup(output)
    with pytest.raises(KeyboardInterrupt):
        _drill(tmp_path, fake).run(_request(tmp_path))

    assert not output.exists()


def test_evidence_invalidation_failure_cannot_accept_previous_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "restore.json"
    output.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")

    def fail_invalidation(path: Path) -> None:
        raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")

    monkeypatch.setattr(restore_drill, "_invalidate_evidence", fail_invalidation)
    assert _drill(tmp_path, _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))).run(_request(tmp_path)) == 2

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "restore_output_invalid"
    assert "TOKEN-SENTINEL" not in output.read_text(encoding="utf-8")


def test_failed_persistence_and_invalidation_cannot_leave_green_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "restore.json"
    output.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")
    clock = _Clock()
    fake = _CleanupObservingRunner(output)
    real_write = restore_drill._write_json
    real_invalidate = restore_drill._invalidate_evidence
    invalidation_calls = 0

    def fail_after_green_publish(path, report):
        if report["measured"] is False:
            raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")
        real_write(path, report)
        clock.now += 3601

    def invalidate_once_then_fail(path: Path) -> None:
        nonlocal invalidation_calls
        invalidation_calls += 1
        if invalidation_calls == 1:
            real_invalidate(path)
            return
        raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")

    monkeypatch.setattr(restore_drill, "_write_json", fail_after_green_publish)
    monkeypatch.setattr(restore_drill, "_invalidate_evidence", invalidate_once_then_fail)

    assert _drill(tmp_path, fake, clock=clock).run(_request(tmp_path)) == 2
    assert invalidation_calls >= 2
    with pytest.raises(ValueError, match=r"rto_seconds_(measurement_unavailable|unmeasured)"):
        read_measurement(output, key="rto_seconds")
    if output.exists():
        assert json.loads(output.read_text(encoding="utf-8"))["measured"] is False
        assert "TOKEN-SENTINEL" not in output.read_text(encoding="utf-8")


def test_initial_invalidation_interruption_cannot_leave_green_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "restore.json"
    output.write_text('{"measured":true,"rto_seconds":37}\n', encoding="utf-8")

    def interrupt_invalidation(path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(restore_drill, "_invalidate_evidence", interrupt_invalidation)

    assert _drill(tmp_path, _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))).run(_request(tmp_path)) == 2
    with pytest.raises(ValueError, match=r"rto_seconds_(measurement_unavailable|unmeasured)"):
        read_measurement(output, key="rto_seconds")


@pytest.mark.parametrize("failure_stage", ["write", "replace"])
def test_success_persistence_failure_leaves_measurement_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    output = tmp_path / "restore.json"
    fake = _CleanupObservingRunner(output)
    real_write = restore_drill._write_json
    real_replace = os.replace

    def fail_write(path, report):
        if report["measured"] is True and failure_stage == "write":
            raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")
        real_write(path, report)

    def fail_replace(source, destination):
        if json.loads(Path(source).read_text(encoding="utf-8"))["measured"] is True:
            raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")
        real_replace(source, destination)

    monkeypatch.setattr(restore_drill, "_write_json", fail_write)
    monkeypatch.setattr(restore_drill.os, "replace", fail_replace)
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    with pytest.raises(ValueError, match=r"rto_seconds_(measurement_unavailable|unmeasured)"):
        read_measurement(output, key="rto_seconds")
    assert not output.exists() or json.loads(output.read_text())["measured"] is False
    assert "TOKEN-SENTINEL" not in output.read_text(encoding="utf-8") if output.exists() else True
    assert not list(tmp_path.glob(".restore.json.*.tmp"))


def test_cleanup_log_failure_does_not_prevent_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "restore.json"
    fake = _CleanupObservingRunner(output, cleanup_status=2)

    def fail_log(*args):
        raise OSError(errno.ENOSPC, "TOKEN-SENTINEL")

    monkeypatch.setattr(restore_drill, "_write_failure_log", fail_log)
    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2
    assert json.loads(output.read_text())["error_code"] == "cleanup_failed"
    with pytest.raises(ValueError, match="rto_seconds_unmeasured"):
        read_measurement(output, key="rto_seconds")


def test_cleanup_blocking_env_unlink_is_interrupted_with_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    real_unlink = Path.unlink
    real_popen = subprocess.Popen
    env_paths = []
    children = []

    def blocking_unlink(path, *args, **kwargs):
        if path.suffix == ".env":
            time.sleep(2)
        return real_unlink(path, *args, **kwargs)

    def blocking_popen(command, *args, **kwargs):
        if command[0] == sys.executable and "-c" in command:
            command = list(command)
            index = command.index("-c") + 1
            command[index] = "import time; time.sleep(2); " + command[index]
            process = real_popen(command, *args, **kwargs)
            children.append(process)
            return process
        return real_popen(command, *args, **kwargs)

    class SlowComposeCleanup(_FakeRunner):
        def __call__(self, command, *, timeout=None, input_text=None, env=None):
            result = super().__call__(command, timeout=timeout, input_text=input_text, env=env)
            if "rm" in command and "restore-db" in command:
                env_paths.append(Path(command[command.index("--env-file") + 1]))
                clock.now += 29.8  # Only 0.2 seconds remain for env unlink.
            return result

    monkeypatch.setattr(Path, "unlink", blocking_unlink)
    monkeypatch.setattr(restore_drill.subprocess, "Popen", blocking_popen)
    fake = SlowComposeCleanup(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    try:
        started = time.monotonic()
        status = _drill(tmp_path, fake, clock=clock).run(_request(tmp_path))
        elapsed = time.monotonic() - started
        assert elapsed < 1.5, f"cleanup waited for blocking unlink: {elapsed:.2f}s"
        assert status == 2
        report = json.loads((tmp_path / "restore.json").read_text())
        assert report["error_code"] == "cleanup_failed"
        assert report["measured"] is False
        with pytest.raises(ValueError, match="rto_seconds_unmeasured"):
            read_measurement(tmp_path / "restore.json", key="rto_seconds")
        assert children and all(child.poll() is not None for child in children)
        assert "DATABASE-PASSWORD-SENTINEL" not in json.dumps(report)
    finally:
        for path in env_paths:
            real_unlink(path, missing_ok=True)


def test_compose_up_failure_still_cleans_the_attempted_container(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        compose_up_status=2,
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    cleanup = [command for command in fake.commands if "rm" in command and "restore-db" in command]
    assert len(cleanup) == 1
    assert all(
        argument.startswith("bfx-dr-")
        for argument in cleanup[0]
        if argument.startswith("bfx-")
    )


class _HealthTimeoutRunner(_FakeRunner):
    def __init__(self) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.health_timeouts: list[float | None] = []

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None, input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            self.health_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(command, timeout or 0)
        return super().__call__(command, input_text=input_text, env=env)


def test_health_inspect_timeout_uses_remaining_deadline_and_writes_failure(tmp_path: Path) -> None:
    fake = _HealthTimeoutRunner()

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "restore_command_failed"
    assert fake.health_timeouts and 0 < fake.health_timeouts[0] <= 600


def test_verifier_failure_cleans_only_generated_resources_and_redacts_secrets(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(
            ("fake",), 2, "TOKEN-SENTINEL", "DATABASE-PASSWORD-SENTINEL"
        )
    )
    drill = _drill(tmp_path, fake)

    assert drill.run(_request(tmp_path)) == 2

    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    log = (tmp_path / "restore.log").read_text(encoding="utf-8")
    assert json.loads(report)["measured"] is False
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report + log
    assert "DATABASE-PASSWORD-SENTINEL" not in report + log
    assert fake.env_mode == 0o600
    assert {line.partition("=")[0] for line in fake.env_text.splitlines()} == {
        "DR_PROJECT_NAME",
        "DR_VOLUME_NAME",
        "DR_NETWORK_NAME",
        "DR_EGRESS_NETWORK_NAME",
        "DR_CONTAINER_NAME",
        "DR_SQL_ADMIN_ROLE",
        "DR_DATABASE_NAME",
        "DATABASE_URL",
        "BFX_DEPLOYMENT_ENV",
        "DR_ACCOUNT_ID",
        "DR_PROJECTOR_VERSION",
        "DR_TARGET_BACKUP_LABEL",
        "DR_TARGET_TIME",
        "DR_PGBACKREST_SECRET_DIR",
    }
    cleanup = fake.commands[-4:]
    assert all(
        argument.startswith("bfx-dr-")
        for command in cleanup
        for argument in command
        if argument.startswith("bfx-")
    )


def test_noninternal_network_fails_before_restore_or_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        network_internal="false\n",
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "network_not_internal"
    assert not any(_is_verifier(command) for command in fake.commands)


def test_internal_egress_network_fails_before_disconnect_or_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        egress_internal="true\n",
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "network_not_internal"
    assert not any(
        command[:3] == ("docker", "network", "disconnect")
        or _is_verifier(command)
        for command in fake.commands
    )


def test_restore_disconnects_egress_and_proves_absence_before_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 0

    disconnect_index = next(
        index
        for index, command in enumerate(fake.commands)
        if command[:3] == ("docker", "network", "disconnect")
    )
    membership_index = next(
        index
        for index, command in enumerate(fake.commands)
        if command[:3]
        == ("docker", "inspect", "--format={{json .NetworkSettings.Networks}}")
    )
    verifier_index = next(
        index
        for index, command in enumerate(fake.commands)
        if _is_verifier(command)
    )
    assert disconnect_index < membership_index < verifier_index
    assert fake.commands[verifier_index][:2] == ("docker", "run")
    assert fake.commands[verifier_index][fake.commands[verifier_index].index("--network") + 1].endswith("-net")
    assert "DR_EGRESS_NETWORK_NAME=" in fake.env_text


def test_restore_rejects_egress_membership_before_verifier(tmp_path: Path) -> None:
    egress_name = "bfx-dr-20260904t031700z-a1b2c3d4e5f60718-egress"
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        container_networks=json.dumps({egress_name: {}}),
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    assert not any(_is_verifier(command) for command in fake.commands)
    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "restore_output_invalid"


def test_malformed_replay_hash_fails_without_a_success_measurement(tmp_path: Path) -> None:
    replay = json.loads(_replay_report())
    replay["event_hash"] = "not-a-hash"
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, json.dumps(replay), "")
    )

    assert _drill(tmp_path, fake).run(_request(tmp_path)) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "event_hash_invalid"


def test_command_runner_exception_is_redacted_into_failure_evidence(tmp_path: Path) -> None:
    def explode(_: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("TOKEN-SENTINEL")

    secret_dir = _write_valid_secret_dir(tmp_path)
    drill = RestoreDrill(
        command_runner=explode,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request(tmp_path)) == 2

    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report


@pytest.mark.parametrize(
    "invalid",
    ["placeholder", "extension", "section", "outside-section", "bare-cr", "semicolon-comment"],
)
def test_restore_rejects_invalid_secret_before_resource_creation(tmp_path: Path, invalid: str) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )
    secret_dir = _write_valid_secret_dir(tmp_path)
    secret_file = secret_dir / "r2.conf"
    if invalid == "extension":
        secret_file.rename(secret_file.with_suffix(".txt"))
    elif invalid == "section":
        secret_file.write_text(secret_file.read_text().replace("[global]", "[bfx]"))
    elif invalid == "outside-section":
        secret_file.write_text(secret_file.read_text().replace("[global]\n", ""))
    elif invalid == "bare-cr":
        secret_file.write_bytes(secret_file.read_bytes().replace(b"\n", b"\r"))
    elif invalid == "semicolon-comment":
        secret_file.write_bytes(
            secret_file.read_bytes().replace(
                b"[global]\n", b"; not-a-pgbackrest-comment\n[global]\n"
            )
        )
    else:
        secret_file.write_text(secret_file.read_text().replace("opaque-access-key", "<ACCOUNT_ID>"))
    drill = RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request(tmp_path)) == 2
    assert fake.commands == []
    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    assert json.loads(report)["error_code"] == "restore_output_invalid"
    assert "<ACCOUNT_ID>" not in report


def test_restore_secret_preflight_rejects_untracked_config_before_resource_creation(
    tmp_path: Path,
) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
    drill = RestoreDrill(
        command_runner=fake,
        config_path=config,
        secret_dir=_write_valid_secret_dir(tmp_path),
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request(tmp_path)) == 2
    assert fake.commands == []
    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "restore_output_invalid"


@pytest.mark.parametrize("state", ("clean", "unstaged", "staged", "untracked", "wildcard", "symlink"))
def test_restore_secret_preflight_requires_exact_clean_tracked_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "pgbackrest.conf"
    if state == "symlink":
        target = tmp_path / "untracked-target.conf"
        target.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
        config.symlink_to(target)
    else:
        config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ("git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=Test", "-c", "user.email=test@invalid", *args),
            check=True, capture_output=True, text=True,
        )

    git("init", "-q")
    git("add", "pgbackrest.conf")
    git("commit", "-qm", "test fixture")
    if state in {"untracked", "wildcard"}:
        config = repo / ("untracked.conf" if state == "untracked" else "pgbackrest*.conf")
    if state != "clean":
        config.write_text("[global]\nrepo1-type=s3\n# changed\n", encoding="utf-8")
    if state == "staged":
        git("add", "pgbackrest.conf")
    monkeypatch.setattr(restore_drill, "ROOT", repo)
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    drill = RestoreDrill(
        command_runner=fake,
        config_path=config,
        secret_dir=_write_valid_secret_dir(tmp_path),
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request(tmp_path)) == (0 if state == "clean" else 2)
    if state != "clean":
        assert fake.commands == []
        report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
        assert report["error_code"] == "restore_output_invalid"
