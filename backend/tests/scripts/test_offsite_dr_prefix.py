"""Baseline-free restore drill (`restore_drill.py --prefix`), offline contracts.

The restored cluster's newest event_prefix_hashes link for the scope must equal
production's link at the same event_seq; the restored chain must recompute. The
baseline drill keeps its evidence file, allowlists and commands unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# Registers every table the replay verifier reads before pg_engine creates the schema.
import scripts.verify_projection_replay  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
PGBACKREST = ROOT / "deploy/vm/pgbackrest"
CONFIG_PATH = PGBACKREST / "pgbackrest.conf"
ACCOUNT = "3f19d046-5030-494c-9a0a-9573bb890c1f"
RUN_ID = "20261001T091700Z-a1b2c3d4e5f60718"
NET = f"bfx-dr-{RUN_ID.lower()}-net"
LABEL_FULL = "20260927-031700F"
LABEL_DIFF = "20260927-031700F_20261001-031700D"
HEAD_SEQ = 90_210
HEAD_HASH = "c" * 64
IMAGE_LABELS = {
    "org.bfx.postgresql.base-digest": "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
    "org.bfx.pgbackrest.version": "2.59.1",
    "org.bfx.pgbackrest.source-sha256": "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


drill_module = _load("offsite_dr_prefix_drill", PGBACKREST / "restore_drill.py")
evidence = _load("offsite_dr_prefix_evidence", PGBACKREST / "evidence.py")
commands = _load("offsite_dr_prefix_commands", PGBACKREST / "restore_commands.py")


@pytest.fixture(autouse=True)
def clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    original = drill_module._config_is_clean_tracked
    monkeypatch.setattr(drill_module, "_config_is_clean_tracked",
                        lambda path: True if path == CONFIG_PATH else original(path))


def _replay(*, event_head: int = HEAD_SEQ, count: int = 3, matches: bool = True) -> dict[str, Any]:
    names = ("offer_claims", "position_state", "venue_offer_state", "venue_credit_state",
             "projection_heads", "reconcile_observation", "submission_attempts",
             "execution_uncertainties")
    row_counts: dict[str, int] = {"event_log": count}
    hashes: dict[str, str] = {"event_log": "a" * 64}
    diff: dict[str, Any] = {}
    for index, name in enumerate(names, start=1):
        digest = f"{index:x}" * 64
        row_counts[name], hashes[name] = index, digest
        diff[name] = {"old_count": index, "replayed_count": index, "old_hash": digest,
                      "replayed_hash": digest, "matches": matches or name != names[0]}
    return {"account_id": ACCOUNT, "environment": "prod", "projector_version": "execution-state-v1",
            "event_head": event_head, "event_hash": "a" * 64, "row_counts": row_counts,
            "content_hashes": hashes, "diagnostic_diff": diff}


def _verification(**prefix: Any) -> str:
    head = {"event_seq": HEAD_SEQ, "prefix_hash": HEAD_HASH, "chain_length": 3, **prefix}
    return json.dumps({"replay": _replay(), "prefix": head})


# --------------------------------------------------------------------------- pure comparison


@pytest.mark.parametrize(("production", "expected"), [
    (f"{HEAD_HASH}\t{HEAD_SEQ}\n", {"production_event_head": HEAD_SEQ}),
    (f"{HEAD_HASH}\t{HEAD_SEQ + 40}\n", {"production_event_head": HEAD_SEQ + 40}),  # prod moved on
])
def test_identical_link_at_the_restored_head_passes(production: str, expected: dict[str, int]) -> None:
    result = evidence.compare_prefix_heads(restored_seq=HEAD_SEQ, restored_hash=HEAD_HASH,
                                           production_tsv=production)
    assert result == {**expected, "production_prefix_hash": HEAD_HASH}


@pytest.mark.parametrize(("production", "code"), [
    (f"{'d' * 64}\t{HEAD_SEQ + 5}\n", "prefix_hash_mismatch"),
    (f"\t{HEAD_SEQ - 1}\n", "prefix_ahead_of_production"),      # restore holds events prod lacks
    (f"\t{HEAD_SEQ + 9}\n", "prefix_ahead_of_production"),      # prod has no link at that seq
    ("\t\n", "prefix_ahead_of_production"),                     # prod scope empty
    (f"{HEAD_HASH}\t{HEAD_SEQ - 1}\n", "prefix_ahead_of_production"),
    ("garbage", "production_read_failed"),
    (f"{HEAD_HASH}\t{HEAD_SEQ}\nextra\t1\n", "production_read_failed"),
    (f"NOT-HEX\t{HEAD_SEQ}\n", "production_read_failed"),
])
def test_prefix_comparison_fails_closed(production: str, code: str) -> None:
    with pytest.raises(evidence.EvidenceError, match=f"^{code}$"):
        evidence.compare_prefix_heads(restored_seq=HEAD_SEQ, restored_hash=HEAD_HASH,
                                      production_tsv=production)


@pytest.mark.parametrize(("verification", "code"), [
    (_verification(chain_length=2), "prefix_chain_invalid"),         # chain vs restored count
    (_verification(event_seq=HEAD_SEQ - 1), "prefix_chain_invalid"),  # chain head vs replay head
    (_verification(prefix_hash="xyz"), "prefix_chain_invalid"),
    (json.dumps({"replay": _replay(), "prefix": {"event_seq": HEAD_SEQ}}), "prefix_chain_invalid"),
    (json.dumps({"replay": _replay(matches=False), "prefix": {
        "event_seq": HEAD_SEQ, "prefix_hash": HEAD_HASH, "chain_length": 3}}), "projection_replay_mismatch"),
    (json.dumps({"replay": _replay(), "prefix": {"event_seq": HEAD_SEQ, "prefix_hash": HEAD_HASH,
                 "chain_length": 3}, "extra": 1}), "restore_output_invalid"),
])
def test_verifier_output_is_bound_to_the_restored_schema_and_replay(verification: str, code: str) -> None:
    with pytest.raises(evidence.EvidenceError, match=f"^{code}$"):
        evidence.parse_prefix_verification(verification, event_count=3, account_id=ACCOUNT,
                                           environment="prod", projector_version="execution-state-v1")


def test_verifier_output_for_another_scope_is_rejected() -> None:
    with pytest.raises(evidence.EvidenceError, match=r"^restore_output_invalid$"):
        evidence.parse_prefix_verification(_verification(), event_count=3, account_id=ACCOUNT,
                                           environment="shadow", projector_version="execution-state-v1")


def test_prefix_failure_codes_do_not_leak_into_baseline_receipts() -> None:
    report = evidence.render_failure_evidence(kind="restore_prefix", error_code="prefix_hash_mismatch",
                                              observed_at_ms=1)
    assert report["kind"] == "restore_prefix" and report["error_code"] == "prefix_hash_mismatch"
    for kind in ("restore", "archive_restore"):
        with pytest.raises(evidence.EvidenceError):
            evidence.render_failure_evidence(kind=kind, error_code="prefix_hash_mismatch", observed_at_ms=1)


# --------------------------------------------------------------------------- chain recomputation


class _Row:
    def __init__(self, event_seq: int) -> None:
        self.event_seq = event_seq


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> Any:
    """prefix_verify.verify_chain with a transparent link function (the real one is
    exercised end to end by the PostgreSQL test below)."""
    from bfx_funding_bot.modules.execution.event_store import canonical

    monkeypatch.setattr(canonical, "rolling_prefix_hash",
                        lambda previous, row: f"{previous}>{row.event_seq}")
    monkeypatch.setattr(canonical, "GENESIS_PREFIX_HASH", "g")
    return _load("offsite_dr_prefix_verify", PGBACKREST / "prefix_verify.py")


def test_chain_head_is_the_last_recomputed_link(chain: Any) -> None:
    rows = [_Row(3), _Row(7), _Row(9)]
    stored = {3: "g>3", 7: "g>3>7", 9: "g>3>7>9"}
    assert chain.verify_chain(rows, stored) == {"event_seq": 9, "prefix_hash": "g>3>7>9",
                                                "chain_length": 3}


@pytest.mark.parametrize(("stored", "code"), [
    ({3: "g>3", 7: "tampered", 9: "g>3>7>9"}, "prefix_chain_mismatch"),
    ({3: "g>3", 9: "g>3>7>9"}, "prefix_chain_mismatch"),                       # missing link
    ({3: "g>3", 7: "g>3>7", 9: "g>3>7>9", 11: "g>3>7>9>11"}, "prefix_chain_incomplete"),  # extra
])
def test_chain_that_does_not_recompute_is_refused(chain: Any, stored: dict[int, str], code: str) -> None:
    with pytest.raises(chain.ChainError, match=f"^{code}$"):
        chain.verify_chain([_Row(3), _Row(7), _Row(9)], stored)


def test_empty_scope_is_refused(chain: Any) -> None:
    with pytest.raises(chain.ChainError, match=r"^prefix_chain_empty$"):
        chain.verify_chain([], {})


def test_baseline_bootstrap_grants_are_unchanged_by_prefix_mode() -> None:
    seen: list[str] = []

    def runner(command: tuple[str, ...], *, input_text: str | None = None,
               timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        seen.append(input_text or "")
        return subprocess.CompletedProcess(command, 0, "", "")

    drill = drill_module.RestoreDrill(command_runner=runner)
    drill._deadline = drill_module.time.monotonic() + 60
    plan = commands.build_restore_plan(
        account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
        backup_label=LABEL_DIFF, target_time=None, run_id=RUN_ID, database_name="bfx",
        expected_event_hash="a" * 64)
    drill._bootstrap_role(plan, "DATABASE-PASSWORD-SENTINEL")
    drill._bootstrap_role(plan, "DATABASE-PASSWORD-SENTINEL", prefix=True)
    assert "event_prefix_hashes" not in seen[0]
    assert 'public."event_prefix_hashes"' in seen[1]


def test_prefix_receipt_never_shares_a_path_with_the_baseline_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert drill_module.DEFAULT_PREFIX_OUTPUT_PATH.name == "restore-prefix.json"
    assert drill_module.DEFAULT_OUTPUT_PATH.name == "restore.json"
    built: list[Path] = []

    class Capture:
        def __init__(self, *, output_path: Path = drill_module.DEFAULT_OUTPUT_PATH) -> None:
            built.append(output_path)

        def run(self, request: Any) -> int:
            assert request.prefix is True and request.baseline_path is None
            return 0

    monkeypatch.setattr(drill_module, "RestoreDrill", Capture)
    assert drill_module.main(["--prefix", "--account-id", ACCOUNT, "--environment", "prod",
                              "--projector-version", "execution-state-v1"]) == 0
    assert built == [drill_module.DEFAULT_PREFIX_OUTPUT_PATH]


# --------------------------------------------------------------------------- drill orchestration


def _info_json() -> str:
    backup = lambda label, kind, start: {  # noqa: E731
        "label": label, "type": kind, "timestamp": {"start": start, "stop": start + 60}}
    return json.dumps([{
        "name": "bfx", "status": {"code": 0},
        "repo": [{"key": 1, "cipher": "aes-256-cbc", "status": {"code": 0}}],
        "backup": [backup(LABEL_FULL, "full", 1_790_000_000), backup(LABEL_DIFF, "diff", 1_790_300_000)],
    }])


class FakeDocker:
    def __init__(self, *, production: str = f"{HEAD_HASH}\t{HEAD_SEQ + 12}\n",
                 verifier: tuple[int, str] | None = None, info_status: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.inputs: dict[tuple[str, ...], str] = {}
        self.env_text = ""
        self.production = production
        self.verifier = verifier or (0, _verification() + "\n")
        self.info_status = info_status

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None,
                 input_text: str | None = None, env: dict[str, str] | None = None,
                 ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if input_text is not None:
            self.inputs[command] = input_text

        def ok(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, code, stdout, "")

        if "pgbackrest" in command and "info" in command:
            return ok(_info_json(), self.info_status)
        if command[:4] == ("docker", "image", "inspect", "--format={{.Id}}"):
            return ok(f"sha256:{'b' * 64}\n")
        if "--env-file" in command and command[:2] == ("docker", "compose"):
            self.env_text = Path(command[command.index("--env-file") + 1]).read_text()
            return ok()
        if command[:3] == ("docker", "network", "inspect"):
            return ok("false\n" if command[-1].endswith("-egress") else "true\n")
        if command[:3] == ("docker", "inspect", "--format={{json .NetworkSettings.Networks}}"):
            return ok(json.dumps({NET: {}}) + "\n")
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            return ok("healthy\n")
        if command[:3] == ("docker", "inspect", "--format={{.Image}}"):
            return ok(f"sha256:{'e' * 64}\n")
        if command[:4] == ("docker", "image", "inspect", "--format={{json .Config.Labels}}"):
            return ok(json.dumps(IMAGE_LABELS))
        if command[:2] == ("docker", "run") and "-" in command:
            code, stdout = self.verifier
            return ok(stdout, code)
        if command[:2] == ("docker", "exec") and input_text is not None:
            if "pg_is_in_recovery" in input_text:
                return ok("f\n")
            if "event_prefix_hashes" in input_text and "READ ONLY" in input_text:
                return ok(self.production)
            if "server_version_num" in input_text:
                return ok("180000\thead-a\t3\n")
            return ok()
        return ok()

    def find(self, predicate: Any) -> tuple[str, ...]:
        return next(call for call in self.calls if predicate(call))


def _prefix_drill(tmp_path: Path, fake: FakeDocker) -> Any:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    secret = secret_dir / "r2.conf"
    secret.write_text("[global]\nrepo1-s3-endpoint=https://a.r2.cloudflarestorage.com\n"
                      "repo1-s3-bucket=b\nrepo1-s3-key=k\nrepo1-s3-key-secret=TOKEN-SENTINEL\n"
                      "repo1-cipher-pass=p\n")
    secret.chmod(0o600)
    secret_dir.chmod(0o700)
    return drill_module.RestoreDrill(
        command_runner=fake, config_path=CONFIG_PATH, secret_dir=secret_dir,
        output_path=tmp_path / "restore-prefix.json", run_id_factory=lambda: RUN_ID,
        password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
        postgres_uid=os.getuid(), postgres_gid=os.getgid(),
    )


def _request() -> Any:
    return drill_module.DrillRequest(
        account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
        backup_label=None, target_time=None, baseline_path=None, prefix=True,
    )


def test_prefix_drill_restores_the_newest_backup_and_compares_with_production(tmp_path: Path) -> None:
    baseline_evidence = tmp_path / "restore.json"
    baseline_evidence.write_text('{"kind":"restore","measured":true}')
    fake = FakeDocker()
    assert _prefix_drill(tmp_path, fake).run(_request()) == 0

    report = json.loads((tmp_path / "restore-prefix.json").read_text())
    assert (report["kind"], report["measured"], report["target_backup_label"]) == (
        "restore_prefix", True, LABEL_DIFF)
    assert report["prefix"] == {"event_seq": HEAD_SEQ, "prefix_hash": HEAD_HASH, "chain_length": 3,
                                "production_event_head": HEAD_SEQ + 12,
                                "production_prefix_hash": HEAD_HASH}
    assert report["target_time"] is None and report["egress_disconnected"] is True
    assert baseline_evidence.read_text() == '{"kind":"restore","measured":true}'   # untouched

    # Newest backup, end-of-archive recovery (no PITR target), no baseline hash anywhere.
    assert f"DR_TARGET_BACKUP_LABEL={LABEL_DIFF}\n" in fake.env_text
    assert "DR_TARGET_TIME=\n" in fake.env_text
    assert fake.calls[0] == ("docker", "exec", "--user", "postgres", "bfx-postgres", "pgbackrest",
                             "--stanza=bfx", "info", "--output=json")
    assert not any("--expected-event-hash" in call for call in fake.calls)
    verifier = fake.find(lambda c: c[:2] == ("docker", "run") and "-" in c)
    assert verifier[:4] == ("docker", "run", "--rm", "-i")
    assert verifier[verifier.index("--network") + 1] == NET
    assert fake.inputs[verifier] == (PGBACKREST / "prefix_verify.py").read_text()
    bootstrap = next(sql for sql in fake.inputs.values() if "CREATE ROLE" in sql)
    assert 'public."event_prefix_hashes"' in bootstrap

    # Production is only read, only for the restored head, and only after isolation.
    production = fake.find(lambda c: c[:2] == ("docker", "exec") and "bfx-postgres" in c
                           and "psql" in c)
    sql = fake.inputs[production]
    assert sql.startswith("BEGIN TRANSACTION READ ONLY;") and sql.rstrip().endswith("ROLLBACK;")
    assert f"event_seq = {HEAD_SEQ} " in sql and f"'{ACCOUNT}'::uuid" in sql
    disconnect = fake.find(lambda c: c[:3] == ("docker", "network", "disconnect"))
    assert fake.calls.index(disconnect) < fake.calls.index(verifier) < fake.calls.index(production)
    assert "DATABASE-PASSWORD-SENTINEL" not in repr(fake.calls)
    # Every generated resource is cleaned up.
    for suffix in ("-data", "-egress", "-net"):
        assert any(c[:3] in {("docker", "volume", "rm"), ("docker", "network", "rm")}
                   and c[-1].endswith(suffix) for c in fake.calls)


@pytest.mark.parametrize(("fake", "code"), [
    (FakeDocker(production=f"{'d' * 64}\t{HEAD_SEQ + 3}\n"), "prefix_hash_mismatch"),
    (FakeDocker(production=f"\t{HEAD_SEQ - 7}\n"), "prefix_ahead_of_production"),
    (FakeDocker(verifier=(3, '{"error": "prefix_chain_mismatch"}\n')), "prefix_chain_invalid"),
    (FakeDocker(verifier=(3, '{"error": "replay_failed", "type": "ValueError"}\n')),
     "restore_command_failed"),
    (FakeDocker(verifier=(0, _verification(chain_length=9) + "\n")), "prefix_chain_invalid"),
])
def test_prefix_drill_failures_write_unmeasured_evidence_and_still_clean_up(
    tmp_path: Path, fake: FakeDocker, code: str,
) -> None:
    assert _prefix_drill(tmp_path, fake).run(_request()) == 2
    report = json.loads((tmp_path / "restore-prefix.json").read_text())
    assert (report["measured"], report["kind"], report["error_code"]) == (False, "restore_prefix", code)
    assert (tmp_path / "restore-prefix.log").read_text() == f"restore drill failed: {code}\n"
    assert not (tmp_path / "restore.log").exists()
    assert any(c[:3] == ("docker", "volume", "rm") for c in fake.calls)


def test_unreadable_backup_catalog_fails_before_any_resource_exists(tmp_path: Path) -> None:
    fake = FakeDocker(info_status=1)
    assert _prefix_drill(tmp_path, fake).run(_request()) == 2
    report = json.loads((tmp_path / "restore-prefix.json").read_text())
    assert report["error_code"] == "backup_label_unavailable"
    assert not any(c[:3] in {("docker", "network", "create"), ("docker", "volume", "create")}
                   for c in fake.calls)


@pytest.mark.parametrize("field", ["target_time", "baseline_path", "archive_only"])
def test_prefix_request_cannot_carry_baseline_inputs(tmp_path: Path, field: str) -> None:
    value = {"target_time": "2026-10-01T04:00:00Z", "baseline_path": tmp_path / "b.json",
             "archive_only": True}[field]
    request = replace(_request(), **{field: value})
    fake = FakeDocker()
    assert _prefix_drill(tmp_path, fake).run(request) == 2
    assert not any(c[:3] == ("docker", "network", "create") for c in fake.calls)


def test_prefix_verifier_command_never_takes_a_baseline_hash() -> None:
    plan = commands.build_restore_plan(
        account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
        backup_label=LABEL_DIFF, target_time=None, run_id=RUN_ID, database_name="bfx",
        expected_event_hash=None)
    assert "--expected-event-hash" not in plan.run_commands[4]
    with pytest.raises(commands.RestoreInputError):
        commands.verifier_command(plan, image="sha256:" + "b" * 64, env_path=Path("/tmp/x.env"))
    baseline_plan = commands.build_restore_plan(
        account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
        backup_label=LABEL_DIFF, target_time=None, run_id=RUN_ID, database_name="bfx",
        expected_event_hash="a" * 64)
    with pytest.raises(commands.RestoreInputError):
        commands.prefix_verifier_command(baseline_plan, image="sha256:" + "b" * 64,
                                         env_path=Path("/tmp/x.env"))


@pytest.mark.parametrize("argv", [
    ["--prefix", "--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v",
     "--baseline", "/tmp/b.json"],
    ["--prefix", "--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v",
     "--target-time", "2026-10-01T04:00:00Z"],
    ["--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v"],
])
def test_cli_keeps_baseline_and_prefix_modes_apart(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        drill_module.main(argv)
    assert exc.value.code == 2


# --------------------------------------------------------------------------- real PostgreSQL


def _container_exec(container: str, *argv: str, input_text: str | None = None) -> str:
    completed = subprocess.run(["docker", "exec", "--user", "postgres", "-i", container, *argv],
                               input=input_text, capture_output=True, text=True, check=False,
                               timeout=120)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _run_prefix_verify(database_url: str) -> subprocess.CompletedProcess[str]:
    """Exactly how the drill runs it: the script on stdin to `python -`, cwd = the app root."""
    return subprocess.run(
        [sys.executable, "-", "--account-id", ACCOUNT, "--environment", "prod",
         "--projector-version", "execution-state-v1"],
        input=(PGBACKREST / "prefix_verify.py").read_text(), cwd=ROOT / "backend",
        env={**os.environ, "DATABASE_URL": database_url, "BFX_DEPLOYMENT_ENV": "prod"},
        capture_output=True, text=True, check=False, timeout=300,
    )


@pytest.mark.integration
async def test_backup_restore_and_prefix_comparison_on_real_postgres(pg_container, pg_engine,
                                                                     pg_session_factory) -> None:
    """Write events -> back up -> keep writing -> restore into an isolated DB -> compare.

    SIMULATION NOTE: pgBackRest + R2 cannot run here, so the physical backup/WAL
    restore is simulated with pg_dump/pg_restore into a separate database of the
    same test cluster. Everything after the restore is the production code path:
    the drill's bootstrap SQL (ephemeral read-only role incl. event_prefix_hashes),
    prefix_verify.py fed on stdin, the drill's production read-only query through
    `docker exec ... psql`, and the evidence comparison.
    """
    from decimal import Decimal
    from uuid import UUID

    import psycopg
    from sqlalchemy import text

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved

    container = pg_container.get_wrapped_container().id
    production_db = pg_engine.url.database
    restored_db = "restored_prefix_" + RUN_ID[-8:]
    admin = pg_engine.url.set(drivername="postgresql")

    async def append(timestamps: tuple[int, ...]) -> None:
        async with pg_session_factory() as session:
            for timestamp in timestamps:
                await PostgresEventStore(deployment_environment="prod").append_snapshot(
                    session, VenueSnapshotObserved(
                        account_id=ACCOUNT, environment="prod", query_started_at_ms=timestamp,
                        query_finished_at_ms=timestamp + 1, offers=(), credits=(),
                        wallet_available={"fUST": Decimal(timestamp)},
                        coverage=SnapshotCoverage(True, True, True)))
            await session.commit()

    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=UUID(ACCOUNT), venue="bitfinex", label="dr-prefix"))
        await session.commit()
    async with pg_engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
        await connection.execute(text("INSERT INTO alembic_version VALUES ('c3a639388457')"))
    await append((1_000, 2_000, 3_000))
    _container_exec(container, "pg_dump", "-U", pg_container.username, "-d", production_db,
                    "-Fc", "-f", "/tmp/prefix-backup.dump")
    await append((4_000, 5_000))                 # production keeps writing after the backup
    _container_exec(container, "createdb", "-U", pg_container.username, restored_db)
    _container_exec(container, "pg_restore", "-U", pg_container.username, "-d", restored_db,
                    "--no-owner", "/tmp/prefix-backup.dump")

    def plan_for(database: str) -> Any:
        return commands.build_restore_plan(
            account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
            backup_label=LABEL_DIFF, target_time=None, run_id=drill_module._new_run_id(),
            database_name=database, expected_event_hash=None)

    restored_plan = plan_for(restored_db)
    password = drill_module._new_password()
    restored_admin = admin.set(database=restored_db).render_as_string(hide_password=False)

    def bootstrap_runner(command: tuple[str, ...], *, input_text: str | None = None,
                         timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        with psycopg.connect(restored_admin, autocommit=True) as connection:
            connection.execute(input_text)
        return subprocess.CompletedProcess(command, 0, "", "")

    bootstrap = drill_module.RestoreDrill(command_runner=bootstrap_runner)
    bootstrap._deadline = drill_module.time.monotonic() + 600
    bootstrap._bootstrap_role(restored_plan, password, prefix=True)
    verifier_url = pg_engine.url.set(
        drivername="postgresql+asyncpg", username=restored_plan.verify_role, password=password,
        database=restored_db).render_as_string(hide_password=False)
    production_admin = admin.render_as_string(hide_password=False)
    try:
        with psycopg.connect(production_admin, autocommit=True) as connection:
            connection.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx') "
                               "THEN CREATE ROLE bfx LOGIN; END IF; END $$")
            # On the VM `bfx` owns the schema; here it is a plain role that may only read.
            connection.execute("GRANT USAGE ON SCHEMA public TO bfx")
            connection.execute("GRANT SELECT ON public.event_prefix_hashes TO bfx")
            production_head = connection.execute(
                "SELECT max(event_seq) FROM event_prefix_hashes").fetchone()[0]

        verified = _run_prefix_verify(verifier_url)
        assert verified.returncode == 0, verified.stdout + verified.stderr
        lines = [line for line in verified.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        _, head = evidence.parse_prefix_verification(
            lines[0], event_count=3, account_id=ACCOUNT, environment="prod",
            projector_version="execution-state-v1")
        assert head["chain_length"] == 3 and head["event_seq"] < production_head

        # The drill's own read-only production query, through docker exec + psql.
        reader = drill_module.RestoreDrill()
        reader._deadline = drill_module.time.monotonic() + 600
        request = replace(_request(), production_container=container)
        production_tsv = reader._production_prefix(request, plan_for(production_db),
                                                    int(head["event_seq"]))
        compared = evidence.compare_prefix_heads(
            restored_seq=int(head["event_seq"]), restored_hash=str(head["prefix_hash"]),
            production_tsv=production_tsv)
        assert compared == {"production_event_head": production_head,
                            "production_prefix_hash": head["prefix_hash"]}

        # A restored chain that does not recompute is refused inside the verifier.
        with psycopg.connect(restored_admin, autocommit=True) as connection:
            connection.execute("UPDATE event_prefix_hashes SET prefix_hash = repeat('0', 64) "
                               "WHERE event_seq = %s", (head["event_seq"],))
        tampered = _run_prefix_verify(verifier_url)
        assert tampered.returncode == 3
        assert json.loads(tampered.stdout.splitlines()[-1]) == {"error": "prefix_chain_mismatch"}
        # ...and a restored link that differs from production is caught by the comparison.
        with pytest.raises(evidence.EvidenceError, match=r"^prefix_hash_mismatch$"):
            evidence.compare_prefix_heads(restored_seq=int(head["event_seq"]),
                                          restored_hash="0" * 64, production_tsv=production_tsv)
    finally:
        with psycopg.connect(production_admin, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{restored_db}" WITH (FORCE)')
            connection.execute(f'DROP ROLE IF EXISTS "{restored_plan.verify_role}"')
            connection.execute("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx') "
                               "THEN EXECUTE 'DROP OWNED BY bfx'; DROP ROLE bfx; END IF; END $$")
        _container_exec(container, "rm", "-f", "/tmp/prefix-backup.dump")
