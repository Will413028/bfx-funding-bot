"""Baseline-free restore drill (`restore_drill.py --restore-test`), offline contracts.

The restored copy's append-only ledger rows must equal production's within the restored
copy's own boundary, and the image's boot check must accept the copy. The baseline drill
keeps its evidence file, allowlists and commands unchanged. The SQL itself runs against
PostgreSQL in tests/integration/test_ledger_restore_verification.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bfx_funding_bot.modules.ledger import table_digest as td

ROOT = Path(__file__).resolve().parents[3]
PGBACKREST = ROOT / "deploy/vm/pgbackrest"
CONFIG_PATH = PGBACKREST / "pgbackrest.conf"
ACCOUNT = "3f19d046-5030-494c-9a0a-9573bb890c1f"
BASIS = "6a0c3c7e-0f43-4a8e-9b1f-1d7e2a3b4c5d"
PENDING = "0b5f0a52-6a5e-4d9e-8c1e-6c2b8a9d0e1f"
RUN_ID = "20261001T091700Z-a1b2c3d4e5f60718"
NET = f"bfx-dr-{RUN_ID.lower()}-net"
DB_CONTAINER = f"bfx-dr-{RUN_ID.lower()}-db"
LABEL_FULL = "20260927-031700F"
LABEL_DIFF = "20260927-031700F_20261001-031700D"
HEAD = "f6a7b8c9d0e1"
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


drill_module = _load("offsite_dr_ledger_drill", PGBACKREST / "restore_drill.py")
evidence = drill_module._evidence
commands = drill_module._commands
ledger = drill_module._ledger


@pytest.fixture(autouse=True)
def clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    original = drill_module._config_is_clean_tracked
    monkeypatch.setattr(drill_module, "_config_is_clean_tracked",
                        lambda path: True if path == CONFIG_PATH else original(path))


def _bounds_output(*, scope: str | None = None, epoch: str = "2\tledger", pending: bool = True,
                   tables: tuple[str, ...] = ledger.LEDGER_TABLES, clock_behind: int = 0) -> str:
    lines = [f"schema\t180000\t{HEAD}", *(f"table\t{name}" for name in tables),
             scope if scope is not None else f"scope\t{ACCOUNT}\tprod\t5\t5\t4\t3\t1\t7",
             *((f"pending\t{PENDING}",) if pending else ()), f"epoch\t{epoch}",
             f"inconsistent\tclock_behind\t{clock_behind}",
             "inconsistent\tvenue_offer_mirror_orphans\t0",
             "inconsistent\tvenue_credit_mirror_orphans\t0"]
    return "\n".join(lines) + "\n"


def _bounds(**kwargs: Any) -> Any:
    return ledger.parse_bounds(_bounds_output(**kwargs))


ROWS = {
    "ledger_observation_query": [f"q{i}\t{ACCOUNT}\tprod" for i in range(5)],
    "ledger_observation_wallet": ["o1\tfunding\tUST\t1.10\t2.5", "o1\tfunding\tBTC\t0\t0"],
    "capital_authority_epoch": ["1\tlegacy", "2\tledger"],
}


def _stream_lines(*, clock: int, rows: dict[str, list[str]] = ROWS, restored: bool,
                  extra: tuple[str, ...] = ()) -> list[bytes]:
    lines = [f"{table}\t{row}" for table, table_rows in rows.items() for row in table_rows]
    lines += list(extra)
    lines.append(f"#clock\t{ACCOUNT}\tprod\t{clock}")
    if restored:
        counts = {name: len(rows.get(name, ())) for name in ledger.LEDGER_TABLES}
        counts["execution_resolution_journal"] = 3  # rows naming the latest accepted observation
        lines += [f"#count\t{name}\t{count}" for name, count in counts.items()]
    return [line.encode() + b"\n" for line in lines]


def _digests(*, production_rows: dict[str, list[str]] = ROWS, production_clock: int = 9,
             restored_rows: dict[str, list[str]] = ROWS) -> tuple[Any, Any, Any]:
    bounds = _bounds()
    restored = ledger.StreamDigest(ledger.compared_tables(bounds))
    production = ledger.StreamDigest(ledger.compared_tables(bounds))
    for line in _stream_lines(clock=7, rows=restored_rows, restored=True):
        restored(line)
    for line in _stream_lines(clock=production_clock, rows=production_rows, restored=False):
        production(line)
    return bounds, restored, production


def _boot(**scope: Any) -> str:
    item = {"exchange_account_id": ACCOUNT, "deployment_environment": "prod", "basis_id": BASIS,
            "reads": [{"symbol": "fUST", "cell_id": "c1", "basis_id": BASIS,
                       "result": "available"}], **scope}
    return json.dumps({"boot": {"schema_head": HEAD, "realm": "prod", "authority": "ledger",
                                "scopes": [item]}}) + "\n"


# --------------------------------------------------------------------------- rules and scripts


def test_every_ledger_table_has_exactly_one_rule_or_is_mutable() -> None:
    """A new ledger table needs a conscious boundary here (or the drill cannot compare it)."""
    assert set(ledger.RULES).isdisjoint(ledger.MUTABLE_TABLES)
    assert set(ledger.RULES) | set(ledger.MUTABLE_TABLES) == set(td.DIGEST_TABLES)
    assert set(ledger.MUTABLE_TABLES) == td.MUTABLE_TABLES
    assert ledger.PARTIAL_TABLES.issubset(ledger.RULES)


def test_every_rule_is_bounded_from_above_and_scoped() -> None:
    for name, rule in ledger.RULES.items():
        assert "<" in rule.where, name
        if name != "capital_authority_epoch":  # the epoch is global
            assert "JOIN b ON" in rule.join, name


def test_both_clusters_run_the_same_read_only_copy_script() -> None:
    bounds = _bounds()
    restored = ledger.digest_script(bounds, restored=True)
    production = ledger.digest_script(bounds, restored=False)
    for script in (restored, production):
        assert script.startswith("SET client_encoding = 'UTF8';\n")
        assert "SET extra_float_digits = 3;\nBEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" in script
        assert script.rstrip().endswith("ROLLBACK;")
        assert not any(word in script.upper() for word in ("INSERT ", "UPDATE ", "DELETE ", "CREATE "))
    copies = [line for line in production.splitlines() if line.startswith("COPY")]
    assert copies == [line for line in restored.splitlines()
                      if line.startswith("COPY") and "'#count'" not in line]
    assert "'#count'" not in production
    assert len(copies) == len(ledger.RULES) + 1  # every compared table plus the clock
    assert f"'{ACCOUNT}'::uuid, 'prod'::text, 5::bigint, 5::bigint, 4::bigint, 3::bigint" in production
    assert f"ARRAY['{PENDING}']::uuid[]" in production
    assert "t.epoch_seq <= 2::bigint" in production
    assert "'{}'::uuid[]" in ledger.digest_script(_bounds(pending=False), restored=False)


def test_a_table_the_restored_schema_lacks_is_skipped_and_reported() -> None:
    present = tuple(name for name in ledger.LEDGER_TABLES if name != "ledger_observation_trade")
    bounds = _bounds(tables=present)
    assert "ledger_observation_trade" not in ledger.compared_tables(bounds)
    assert "public.ledger_observation_trade" not in ledger.digest_script(bounds, restored=True)


@pytest.mark.parametrize(("output", "code"), [
    (_bounds_output().replace(f"schema\t180000\t{HEAD}\n", ""), "ledger_bounds_invalid"),
    (_bounds_output(scope=f"scope\t{ACCOUNT.upper()}\tprod\t1\t1\t1\t1\t1\t1"), "ledger_bounds_invalid"),
    (_bounds_output(scope=f"scope\t{ACCOUNT}\tprod\t-1\t1\t1\t1\t1\t1"), "ledger_bounds_invalid"),
    (_bounds_output(scope=f"scope\t{ACCOUNT}\tProd; DROP\t1\t1\t1\t1\t1\t1"), "ledger_bounds_invalid"),
    (_bounds_output() + "surprise\tline\n", "ledger_bounds_invalid"),
    (_bounds_output() + f"pending\t{PENDING}\n", "ledger_bounds_invalid"),          # duplicate
    (_bounds_output() + f"scope\t{ACCOUNT}\tprod\t1\t1\t1\t1\t1\t1\n", "ledger_bounds_invalid"),
    (_bounds_output().replace("inconsistent\tclock_behind\t0\n", ""), "ledger_bounds_invalid"),
    (_bounds_output(tables=("ledger_observation_wallet",)), "ledger_schema_incomplete"),
])
def test_bounds_output_is_parsed_strictly(output: str, code: str) -> None:
    with pytest.raises(ledger.LedgerVerificationError, match=rf"^{code}$"):
        ledger.parse_bounds(output)


def test_a_restored_copy_without_any_scope_is_refused() -> None:
    bounds = ledger.parse_bounds(_bounds_output().replace(
        f"scope\t{ACCOUNT}\tprod\t5\t5\t4\t3\t1\t7\n", ""))
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_empty$"):
        ledger.digest_script(bounds, restored=True)


# --------------------------------------------------------------------------- digest and comparison


def test_digest_does_not_depend_on_row_order() -> None:
    first = ledger.StreamDigest(("ledger_observation_wallet",))
    second = ledger.StreamDigest(("ledger_observation_wallet",))
    rows = [b"ledger_observation_wallet\to1\tUST\t1.10\n", b"ledger_observation_wallet\to1\tBTC\t0\n"]
    for line in rows:
        first(line)
    for line in reversed(rows):
        second(line)
    assert first.digests() == second.digests()
    third = ledger.StreamDigest(("ledger_observation_wallet",))
    third(b"ledger_observation_wallet\to1\tUST\t1.1\n")  # the stored scale is part of the value
    third(rows[1])
    assert third.digests() != first.digests()


def test_matching_ledger_summarizes_every_table() -> None:
    summary = ledger.compare(*_digests())
    assert summary["tables"]["ledger_observation_wallet"]["count"] == 2
    assert summary["tables"]["execution_resolution_journal"] == {
        "count": 0, "digest": "0" * 64, "bound": "accepted_observation_query_revision",
        "restored_total": 3}
    assert summary["scopes"] == [{
        "exchange_account_id": ACCOUNT, "deployment_environment": "prod", "query_revision": 5,
        "observation_query_revision": 5, "accepted_observation_query_revision": 4,
        "attempt_seq": 3, "opened_revision": 1, "clock_revision": 7,
        "production_clock_revision": 9}]
    assert (summary["pending_attempts"], summary["epoch_seq"], summary["rows_compared"]) == (1, 2, 9)
    assert summary["absent"] == []


@pytest.mark.parametrize(("kwargs", "code", "detail"), [
    ({"production_rows": {**ROWS, "ledger_observation_wallet": ["o1\tfunding\tUST\t1.1\t2.5",
                                                                "o1\tfunding\tBTC\t0\t0"]}},
     "ledger_digest_mismatch", ("ledger_observation_wallet",)),
    ({"production_rows": {**ROWS, "capital_authority_epoch": ["1\tlegacy"]}},
     "ledger_digest_mismatch", ("capital_authority_epoch",)),
    ({"production_clock": 6}, "ledger_ahead_of_production", ()),
    ({"restored_rows": {**ROWS, "ledger_observation_query": ["q0\tx\tprod"]}},
     "ledger_bound_invalid", ("ledger_observation_query",)),
])
def test_comparison_fails_closed(kwargs: dict[str, Any], code: str, detail: tuple[str, ...]) -> None:
    bounds, restored, production = _digests(**kwargs)
    if "restored_rows" in kwargs:  # the totals still say 5 rows: the bound dropped some
        restored.totals["ledger_observation_query"] = 5
    with pytest.raises(ledger.LedgerVerificationError, match=rf"^{code}$") as exc:
        ledger.compare(bounds, restored, production)
    assert exc.value.detail == detail


@pytest.mark.parametrize(("change", "code"), [
    (lambda bounds, restored, production: setattr(production, "invalid", True), "production_read_failed"),
    (lambda bounds, restored, production: restored(b"unknown_table\tx\n"), "restore_output_invalid"),
    (lambda bounds, restored, production: production.clocks.update({("x", "prod"): 1}),
     "restore_output_invalid"),
    (lambda bounds, restored, production: restored.totals.pop("venue_offer_mirror"),
     "restore_output_invalid"),
])
def test_unexpected_stream_content_is_refused(change: Callable[..., Any], code: str) -> None:
    bounds, restored, production = _digests()
    change(bounds, restored, production)
    with pytest.raises(ledger.LedgerVerificationError, match=rf"^{code}$"):
        ledger.compare(bounds, restored, production)


def test_restored_inconsistency_and_non_ledger_epoch_are_refused() -> None:
    _, restored, production = _digests()
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_restored_inconsistent$") as exc:
        ledger.compare(_bounds(clock_behind=1), restored, production)
    assert exc.value.detail == ("clock_behind",)
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_authority_not_ledger$"):
        ledger.compare(_bounds(epoch="2\tlegacy"), restored, production)


# --------------------------------------------------------------------------- boot check output


def test_boot_output_is_bound_to_the_restored_bounds() -> None:
    boot = ledger.parse_boot(_boot(), _bounds())
    assert boot["scopes"][0]["reads"][0]["result"] == "available"
    pending = ledger.parse_boot(_boot(reads=[{"symbol": "fUST", "cell_id": "c1", "basis_id": None,
                                              "result": "blocked:snapshot_query_pending"}]), _bounds())
    assert pending["scopes"][0]["reads"][0]["basis_id"] is None


@pytest.mark.parametrize(("output", "code"), [
    (_boot().replace(HEAD, "a1b2c3d4e5f6"), "boot_schema_head_mismatch"),
    (_boot(deployment_environment="shadow"), "restore_output_invalid"),     # another scope set
    (_boot(reads=[]), "restore_output_invalid"),                            # nothing was read
    (_boot(reads=[{"symbol": "fUST", "cell_id": "c1", "basis_id": None, "result": "available"}]),
     "restore_output_invalid"),                                             # no basis folded
    (_boot().replace('"ledger"', '"legacy"'), "restore_output_invalid"),
    (_boot() + _boot(), "restore_output_invalid"),
])
def test_boot_output_fails_closed(output: str, code: str) -> None:
    with pytest.raises(ledger.LedgerVerificationError, match=rf"^{code}$"):
        ledger.parse_boot(output, _bounds())


@pytest.mark.parametrize(("stdout", "code"), [
    ('{"error": "boot_seed_missing"}\n', "boot_seed_missing"),
    ('{"error": "boot_check_failed", "type": "OSError"}\n', "boot_check_failed"),
    ('{"error": "anything else"}\n', "boot_check_failed"),
    ("not json\n", "boot_check_failed"),
    ("", "boot_check_failed"),
])
def test_boot_refusal_codes_are_bounded(stdout: str, code: str) -> None:
    assert ledger.boot_failure_code(stdout) == code
    assert code in evidence.LEDGER_ERROR_CODES


def test_ledger_failure_codes_do_not_leak_into_baseline_receipts() -> None:
    for kind in ("restore_ledger", "restore_prefix"):
        report = evidence.render_failure_evidence(kind=kind, error_code="ledger_digest_mismatch",
                                                  observed_at_ms=1)
        assert report["kind"] == kind and report["error_code"] == "ledger_digest_mismatch"
    for kind in ("restore", "archive_restore"):
        with pytest.raises(evidence.EvidenceError):
            evidence.render_failure_evidence(kind=kind, error_code="ledger_digest_mismatch",
                                             observed_at_ms=1)


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
    def __init__(self, *, boot: tuple[int, str] | None = None, info_status: int = 0,
                 production_rows: dict[str, list[str]] = ROWS, production_status: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.inputs: dict[tuple[str, ...], str] = {}
        self.env_text = ""
        self.boot = boot or (0, _boot())
        self.info_status = info_status
        self.production_rows = production_rows
        self.production_status = production_status

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
        if command[:2] == ("docker", "run") and command[-1] == "-":
            code, stdout = self.boot
            return ok(stdout, code)
        if command[:2] == ("docker", "exec") and input_text is not None:
            if "pg_is_in_recovery" in input_text:
                return ok("f\n")
            if "SELECT 'scope'" in input_text:
                assert DB_CONTAINER in command  # W comes from the restored copy only
                return ok(_bounds_output())
            return ok()
        return ok()

    def stream(self, command: tuple[str, ...], *, input_text: str, timeout: float,
               consume: Callable[[bytes], None]) -> int:
        self.calls.append(command)
        self.inputs[command] = input_text
        restored = DB_CONTAINER in command
        rows = ROWS if restored else self.production_rows
        for line in _stream_lines(clock=7 if restored else 9, rows=rows, restored=restored):
            consume(line)
        return 0 if restored else self.production_status

    def find(self, predicate: Callable[[tuple[str, ...]], bool]) -> tuple[str, ...]:
        return next(call for call in self.calls if predicate(call))


def _ledger_drill(tmp_path: Path, fake: FakeDocker, *, name: str = "restore-ledger.json") -> Any:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    secret = secret_dir / "r2.conf"
    secret.write_text("[global]\nrepo1-s3-endpoint=https://a.r2.cloudflarestorage.com\n"
                      "repo1-s3-bucket=b\nrepo1-s3-key=k\nrepo1-s3-key-secret=TOKEN-SENTINEL\n"
                      "repo1-cipher-pass=p\n")
    secret.chmod(0o600)
    secret_dir.chmod(0o700)
    return drill_module.RestoreDrill(
        command_runner=fake, stream_runner=fake.stream, config_path=CONFIG_PATH,
        secret_dir=secret_dir, output_path=tmp_path / name, run_id_factory=lambda: RUN_ID,
        password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
        postgres_uid=os.getuid(), postgres_gid=os.getgid(),
    )


def _is_production(call: tuple[str, ...]) -> bool:
    return call[:2] == ("docker", "exec") and "bfx-postgres" in call and "psql" in call


def test_ledger_drill_restores_the_newest_backup_and_compares_with_production(tmp_path: Path) -> None:
    baseline_evidence = tmp_path / "restore.json"
    baseline_evidence.write_text('{"kind":"restore","measured":true}')
    fake = FakeDocker()
    assert _ledger_drill(tmp_path, fake).run(drill_module.LedgerRequest()) == 0

    report = json.loads((tmp_path / "restore-ledger.json").read_text())
    assert (report["kind"], report["measured"], report["target_backup_label"]) == (
        "restore_ledger", True, LABEL_DIFF)
    assert report["restore_test"] is True
    assert (report["server_version_num"], report["migration_heads"]) == (180000, [HEAD])
    assert report["ledger"]["scopes"][0]["production_clock_revision"] == 9
    assert report["ledger"]["rows_compared"] == 9
    assert report["boot"]["scopes"][0]["basis_id"] == BASIS
    assert report["target_time"] is None and report["egress_disconnected"] is True
    assert report["restore_run_id"] == RUN_ID.lower()
    assert "prefix" not in report and "event_count" not in report
    assert baseline_evidence.read_text() == '{"kind":"restore","measured":true}'   # untouched

    # Newest backup, end-of-archive recovery (no PITR target), no baseline hash anywhere.
    assert f"DR_TARGET_BACKUP_LABEL={LABEL_DIFF}\n" in fake.env_text
    assert "DR_TARGET_TIME=\n" in fake.env_text
    assert fake.calls[0] == ("docker", "exec", "--user", "postgres", "bfx-postgres", "pgbackrest",
                             "--stanza=bfx", "info", "--output=json")
    assert not any("--expected-event-hash" in call for call in fake.calls)
    verifier = fake.find(lambda c: c[:2] == ("docker", "run") and c[-1] == "-")
    assert verifier[:4] == ("docker", "run", "--rm", "-i")
    assert verifier[verifier.index("--network") + 1] == NET
    assert fake.inputs[verifier] == (PGBACKREST / "ledger_boot_check.py").read_text()
    bootstrap = next(sql for sql in fake.inputs.values() if "CREATE ROLE" in sql)
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public" in bootstrap
    assert "SET default_transaction_read_only = on" in bootstrap

    # Production is only read, with the restored copy's bounds, and only after isolation.
    production = fake.find(_is_production)
    script = fake.inputs[production]
    assert "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" in script
    assert script.rstrip().endswith("ROLLBACK;") and "'#count'" not in script
    restored_copy = fake.find(lambda c: c[:2] == ("docker", "exec") and DB_CONTAINER in c
                              and "'#count'" in fake.inputs.get(c, ""))
    disconnect = fake.find(lambda c: c[:3] == ("docker", "network", "disconnect"))
    assert fake.calls.index(disconnect) < fake.calls.index(verifier) < fake.calls.index(production)
    assert fake.calls.index(restored_copy) < fake.calls.index(production)
    assert production[production.index("-U") + 1] == "bfx"
    assert "DATABASE-PASSWORD-SENTINEL" not in repr(fake.calls)
    # Every generated resource is cleaned up.
    for suffix in ("-data", "-egress", "-net"):
        assert any(c[:3] in {("docker", "volume", "rm"), ("docker", "network", "rm")}
                   and c[-1].endswith(suffix) for c in fake.calls)


@pytest.mark.parametrize(("fake", "code"), [
    (FakeDocker(production_rows={**ROWS, "ledger_observation_wallet": ["o1\tfunding\tUST\t9\t9",
                                                                       "o1\tfunding\tBTC\t0\t0"]}),
     "ledger_digest_mismatch"),
    (FakeDocker(production_status=3), "production_read_failed"),
    (FakeDocker(boot=(3, '{"error": "boot_seed_missing"}\n')), "boot_seed_missing"),
    (FakeDocker(boot=(3, '{"error": "boot_check_failed", "type": "ImportError"}\n')),
     "boot_check_failed"),
    (FakeDocker(boot=(125, "")), "restore_command_failed"),
    (FakeDocker(boot=(0, _boot(reads=[]))), "restore_output_invalid"),
])
def test_ledger_drill_failures_write_unmeasured_evidence_and_still_clean_up(
    tmp_path: Path, fake: FakeDocker, code: str, capsys: pytest.CaptureFixture[str],
) -> None:
    assert _ledger_drill(tmp_path, fake).run(drill_module.LedgerRequest()) == 2
    report = json.loads((tmp_path / "restore-ledger.json").read_text())
    assert (report["measured"], report["kind"], report["error_code"]) == (False, "restore_ledger", code)
    assert (tmp_path / "restore-ledger.log").read_text() == f"restore drill failed: {code}\n"
    assert not (tmp_path / "restore.log").exists()
    assert any(c[:3] == ("docker", "volume", "rm") for c in fake.calls)
    if code == "ledger_digest_mismatch":  # table names (never row data) go to the journal
        assert "ledger_digest_mismatch: ledger_observation_wallet" in capsys.readouterr().err


def test_unreadable_backup_catalog_fails_before_any_resource_exists(tmp_path: Path) -> None:
    fake = FakeDocker(info_status=1)
    assert _ledger_drill(tmp_path, fake).run(drill_module.LedgerRequest()) == 2
    report = json.loads((tmp_path / "restore-ledger.json").read_text())
    assert report["error_code"] == "backup_label_unavailable"
    assert not any(c[:3] in {("docker", "network", "create"), ("docker", "volume", "create")}
                   for c in fake.calls)


def test_transitional_prefix_receipt_satisfies_the_previous_wrapper(tmp_path: Path) -> None:
    """The release before this one installed a wrapper that runs `--prefix ...` and accepts
    only a fresh, measured `restore_prefix` receipt; it still gets one, from ledger mode."""
    fake = FakeDocker()
    request = drill_module.LedgerRequest(kind="restore_prefix")
    assert _ledger_drill(tmp_path, fake, name="restore-prefix.json").run(request) == 0
    report = json.loads((tmp_path / "restore-prefix.json").read_text())
    assert (report["measured"], report["kind"]) == (True, "restore_prefix")
    assert type(report["observed_at_ms"]) is int and "ledger" in report and "boot" in report


def test_bootstrap_grants_are_per_mode() -> None:
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
    drill._bootstrap_role(plan, "DATABASE-PASSWORD-SENTINEL", ledger=True)
    assert 'public."event_log"' in seen[0] and "ALL TABLES" not in seen[0]
    assert "ALL TABLES IN SCHEMA public" in seen[1] and "event_log" not in seen[1]


def test_ledger_verifier_command_runs_the_script_on_the_isolated_network() -> None:
    resources = commands.build_restore_resources(
        backup_label=LABEL_DIFF, target_time=None, run_id=RUN_ID, database_name="bfx")
    command = commands.ledger_verifier_command(resources, image="sha256:" + "b" * 64,
                                               env_path=Path("/tmp/x.env"))
    assert command[-3:] == ("python", "sha256:" + "b" * 64, "-")
    assert command[command.index("--network") + 1] == NET
    with pytest.raises(commands.RestoreInputError):
        commands.ledger_verifier_command(resources, image="bfx-bot:local", env_path=Path("/tmp/x.env"))
    with pytest.raises(commands.RestoreInputError):  # the baseline replay always pins a hash
        commands.build_restore_plan(
            account_id=ACCOUNT, environment="prod", projector_version="execution-state-v1",
            backup_label=LABEL_DIFF, target_time=None, run_id=RUN_ID, database_name="bfx",
            expected_event_hash=None)


# --------------------------------------------------------------------------- CLI


@pytest.mark.parametrize(("argv", "kind", "output"), [
    (["--restore-test"], "restore_ledger", "DEFAULT_LEDGER_OUTPUT_PATH"),
    (["--restore-test", "--output", "/srv/evidence/receipt.json"], "restore_ledger",
     Path("/srv/evidence/receipt.json")),
    (["--prefix", "--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v"],
     "restore_prefix", "LEGACY_PREFIX_OUTPUT_PATH"),
])
def test_cli_routes_the_restore_test_and_the_transitional_prefix_call_to_ledger_mode(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], kind: str, output: str | Path,
) -> None:
    assert drill_module.DEFAULT_LEDGER_OUTPUT_PATH.name == "restore-ledger.json"
    assert drill_module.DEFAULT_OUTPUT_PATH.name == "restore.json"
    built: list[tuple[Path, Any]] = []

    class Capture:
        def __init__(self, *, output_path: Path = drill_module.DEFAULT_OUTPUT_PATH) -> None:
            self.output_path = output_path

        def run(self, request: Any) -> int:
            built.append((self.output_path, request))
            return 0

    monkeypatch.setattr(drill_module, "RestoreDrill", Capture)
    assert drill_module.main(argv) == 0
    [(path, request)] = built
    assert path == (output if isinstance(output, Path) else getattr(drill_module, output))
    assert request == drill_module.LedgerRequest(kind=kind)


@pytest.mark.parametrize("argv", [
    ["--restore-test", "--account-id", ACCOUNT],
    ["--restore-test", "--baseline", "/tmp/b.json"],
    ["--restore-test", "--target-time", "2026-10-01T04:00:00Z"],
    ["--restore-test", "--output", "relative.json"],
    ["--prefix", "--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v",
     "--output", "/tmp/x.json"],
    ["--restore-test", "--prefix", "--account-id", ACCOUNT, "--environment", "prod",
     "--projector-version", "v"],
    ["--prefix"],
    ["--prefix", "--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v",
     "--backup-label", LABEL_DIFF],
    ["--account-id", ACCOUNT, "--environment", "prod", "--projector-version", "v"],
])
def test_cli_keeps_baseline_and_restore_test_modes_apart(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        drill_module.main(argv)
    assert exc.value.code == 2


# --------------------------------------------------------------------------- streaming runner


def test_stream_runner_feeds_stdin_and_streams_every_line() -> None:
    lines: list[bytes] = []
    status = drill_module._stream_command(
        (sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"),
        input_text="ledger\tone\nledger\ttwo\n", timeout=60, consume=lines.append)
    assert status == 0
    assert lines == [b"LEDGER\tONE\n", b"LEDGER\tTWO\n"]


def test_stream_runner_kills_a_reader_that_outlives_its_budget() -> None:
    status = drill_module._stream_command(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        input_text="", timeout=0.5, consume=lambda line: None)
    assert status != 0
