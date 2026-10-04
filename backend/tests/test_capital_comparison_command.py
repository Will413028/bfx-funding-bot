"""Command safety boundaries, JSONL coverage and failure classification."""

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from bfx_funding_bot.apps import capital_comparison as command
from bfx_funding_bot.apps.capital_comparison_guard import (
    READER_ROLE,
    ConnectionPlan,
    GuardRejectedError,
    validate_connection,
    verify_connection,
)
from bfx_funding_bot.apps.capital_comparison_inventory import InventoryResult
from bfx_funding_bot.modules.trading import Blocked, CapitalScope
from bfx_funding_bot.modules.trading_shadow import ComparisonHeads, ShadowComparison

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CELLS = Path(__file__).parents[1] / "configs/cells.live.yaml"


@pytest.fixture
def arguments(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "mode": "rehearsal", "host": "bfx-dr-test-db", "port": 5432,
        "database": "copy", "user": "reader", "run_id": "test", "now_ms": 2000,
    }))
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text("postgresql://reader:secret@bfx-dr-test-db:5432/copy\n")
    dsn_file.chmod(0o600)
    return ["--mode", "rehearsal", "--dsn-file", str(dsn_file),
            "--manifest", str(manifest), "--run-id", "test", "--code-revision", "test-revision",
            "--cells", str(CELLS), "--scope", f"{ACCOUNT}:ci"]


def change(args, flag, value):
    args[args.index(flag) + 1] = value


@pytest.mark.parametrize("fault", [
    "host", "port", "database", "user", "run_id", "query", "multi_host", "socket",
    "empty_manifest", "malformed_manifest", "manifest_list", "missing_field", "missing_mode",
    "wrong_mode", "cutover_without_authorization", "cutover_without_manifest",
    "rehearsal_authorization", "manifest_run", "service", "encoded_host", "no_port",
])
async def test_guard_rejection_never_connects(arguments, fault):
    if fault in {"host", "port", "database", "user", "query", "multi_host", "socket",
                 "service", "encoded_host", "no_port"}:
        dsn_file = Path(arguments[arguments.index("--dsn-file") + 1])
        dsn = dsn_file.read_text().strip()
        changes = {
            "host": dsn.replace("bfx-dr-test-db", "production.example"),
            "port": dsn.replace(":5432", ":5433"),
            "database": dsn.replace("/copy", "/production"),
            "user": dsn.replace("reader:", "owner:"),
            "query": dsn + "?host=production.example",
            "multi_host": dsn.replace("bfx-dr-test-db", "bfx-dr-test-db,production.example"),
            "socket": "postgresql://reader:secret@/copy?host=/tmp",
            "service": dsn + "?service=production",
            "encoded_host": dsn.replace("bfx-dr-test-db", "%2ftmp"),
            "no_port": dsn.replace(":5432", ""),
        }
        dsn_file.write_text(changes[fault])
    elif fault in {"empty_manifest", "malformed_manifest", "manifest_list", "missing_field", "manifest_run"}:
        path = Path(arguments[arguments.index("--manifest") + 1])
        data = json.loads(path.read_text())
        data.pop("database") if fault == "missing_field" else None
        if fault == "manifest_run":
            data["run_id"] = "other"
        path.write_text({"empty_manifest": "{}", "malformed_manifest": "{",
                         "manifest_list": "[]"}.get(fault, json.dumps(data)))
    elif fault == "run_id":
        change(arguments, "--run-id", "other")
    elif fault == "missing_mode":
        del arguments[:2]
    elif fault == "wrong_mode":
        change(arguments, "--mode", "secret-invalid-mode")
    elif fault.startswith("cutover"):
        change(arguments, "--mode", "cutover")
        if fault == "cutover_without_manifest":
            arguments.append("--authorize-cutover-read")
    else:
        arguments.append("--authorize-cutover-read")
    attempts = []

    def connector(plan):
        attempts.append(plan)
        raise AssertionError("must not connect")

    output = io.StringIO()
    assert await command.run(arguments, output=output, connector=connector) == 3
    assert attempts == []
    assert "secret" not in output.getvalue() and "postgresql" not in output.getvalue()


_SCAN_REASONS = (
    "table_write_privilege", "column_write_privilege", "sequence_privilege",
    "database_create_privilege", "schema_create_privilege", "owned_relation",
    "owned_function", "owned_schema", "security_definer_executable", "forbidden_extension",
)


class Session:
    """Answers the attestation queries as a healthy reader session unless a fault is set."""

    def __init__(self, identity=("reader", "copy", "repeatable read", "on"), fault=None):
        self.identity = identity
        self.fault = fault
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self

    async def execute(self, query, params=None):
        sql = str(query)
        self.statements.append(sql)
        if "session_user, current_database()" in sql:
            return SimpleNamespace(one=lambda: self.identity)
        if "FROM closure c JOIN pg_roles" in sql:
            attributes = (self.fault == "attribute",) + (False,) * 4
            login = ("reader", False, self.fault == "login_inherits", *attributes)
            reader = (READER_ROLE, self.fault == "reader_can_login", False, *((False,) * 5))
            rows = [login, reader]
            if self.fault == "closure":
                rows.append(("bfx_bot", False, False, *((False,) * 5)))
            return SimpleNamespace(all=lambda: rows)
        if "capital_policy_heads" in sql:
            return SimpleNamespace(first=lambda: (1, UUID(ACCOUNT)))
        return None

    async def scalar(self, query, params=None):
        sql = str(query)
        self.statements.append(sql)
        if "SELECT current_user" in sql:
            return "reader" if self.fault == "role_switch" else READER_ROLE
        return None

    async def scalars(self, query, params=None):
        sql = str(query)
        self.statements.append(sql)
        found = ["public.x"] if self.fault == _fault_for(sql) else []
        return SimpleNamespace(all=lambda: found)

    async def begin_nested(self):
        return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())


def _fault_for(sql):
    markers = {
        "has_table_privilege": "table_write_privilege",
        "has_any_column_privilege": "column_write_privilege",
        "has_sequence_privilege": "sequence_privilege",
        "has_database_privilege": "database_create_privilege",
        "has_schema_privilege": "schema_create_privilege",
        "c.relowner": "owned_relation",
        "p.proowner": "owned_function",
        "n.nspowner": "owned_schema",
        "prosecdef": "security_definer_executable",
        "pg_extension": "forbidden_extension",
    }
    return next((reason for marker, reason in markers.items() if marker in sql), None)


@pytest.fixture(autouse=True)
def clean_inventory(monkeypatch):
    async def inventory(session, *, scopes, policy_without_cell):
        return InventoryResult("ok", ())

    monkeypatch.setattr(command, "reverse_inventory", inventory)


def comparison(status="equal", reason=None):
    blocked = Blocked(reason, ()) if reason else None
    return ShadowComparison("fold_comparison", status, blocked, blocked, (), (), None, None,
                            False, None, ComparisonHeads(), None)


_ATTESTATION_FAULTS = {
    "user": "connection_attestation_failed",
    "database": "connection_attestation_failed",
    "isolation": "connection_attestation_failed",
    "readonly": "connection_attestation_failed",
    "role_switch": "reader_role_unavailable",
    "closure": "role_closure_unexpected",
    "attribute": "role_attribute_privileged",
    "reader_can_login": "reader_can_login",
    "login_inherits": "login_inherits",
    **{reason: reason for reason in _SCAN_REASONS},
}


@pytest.mark.parametrize("fault", list(_ATTESTATION_FAULTS))
async def test_post_connect_refusal_precedes_business_queries(arguments, monkeypatch, fault):
    identity = ["reader", "copy", "repeatable read", "on"]
    if fault in {"user", "database", "isolation", "readonly"}:
        identity[["user", "database", "isolation", "readonly"].index(fault)] = "wrong"
    session = Session(tuple(identity), fault)
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: session)
    attempts = []

    def connector(plan):
        attempts.append(plan)
        return SimpleNamespace(dispose=AsyncMock())

    output = io.StringIO()
    assert await command.run(arguments, output=output, connector=connector) == 3
    assert len(attempts) == 1  # Actual identity/ACL cannot be checked before a connection.
    assert json.loads(output.getvalue())["reason"] == _ATTESTATION_FAULTS[fault]
    assert not any("FROM public.capital_policy_heads" in sql for sql in session.statements)


@pytest.mark.parametrize(("status", "reason", "exit_code"), [
    ("equal", None, 0), ("different", None, 1), ("not_comparable", None, 1),
    ("error", None, 1), ("equal", "snapshot_stale", 1),
    ("equal", "snapshot_evidence_missing", 1), ("equal", "execution_unknown", 0),
])
async def test_run_jsonl_and_exit(arguments, monkeypatch, status, reason, exit_code):
    session = Session()
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: session)
    comparator = AsyncMock(return_value=comparison(status, reason))
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: comparator)
    output = io.StringIO()
    assert await command.run(arguments, output=output,
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == exit_code
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(rows) == 4
    assert rows.pop(0)["kind"] == "inventory"
    assert rows[-1]["coverage_complete"] is True
    assert rows[-1]["expected_scopes"] == rows[-1]["emitted_scopes"] == 2
    assert {row["scope"]["cell_id"] for row in rows[:-1]} == {"fUST_a30", "fUST_p2"}
    assert all(row["provenance"]["now_ms"] == 2000 for row in rows)
    assert all({"scope", "heads", "status", "reason", "differences", "classifications",
                "evidence", "digests", "provenance"} <= row.keys() for row in rows[:-1])
    assert all(call.args[0] is session for call in comparator.call_args_list)
    assert session.statements.count("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY") == 1


async def test_output_and_connection_errors_are_sanitized(arguments, monkeypatch):
    def failed(plan):
        raise RuntimeError("postgresql://reader:secret@host SQL raw secret")

    output = io.StringIO()
    assert await command.run(arguments, output=output, connector=failed) == 3
    assert "secret" not in output.getvalue()
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: Session())
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: AsyncMock(return_value=comparison()))

    class BrokenOutput:
        def write(self, value):
            raise OSError("secret")

    assert await command.run(arguments, output=BrokenOutput(),
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == 3


def test_summary_empty_missing_duplicate_and_mixed():
    scope = CapitalScope(UUID(ACCOUNT), "ci", "fUST", "fUST_a30")
    ok = InventoryResult("ok", ())
    assert command.summarize([], [], inventory=ok)["exit_code"] == 1
    assert command.summarize([scope], [], inventory=ok)["exit_code"] == 1
    assert command.summarize([scope, scope], [comparison(), comparison()], inventory=ok)["exit_code"] == 1
    assert command.summarize([scope, replace(scope, cell_id="fUST_p2")],
                             [comparison(), comparison(reason="snapshot_stale")],
                             inventory=ok)["exit_code"] == 0
    violated = InventoryResult("not_comparable", ({"reason": "scope_unlisted"},))
    both = [scope, replace(scope, cell_id="fUST_p2")]
    summary = command.summarize(both, [comparison(), comparison()], inventory=violated)
    assert (summary["exit_code"], summary["inventory_status"]) == (1, "not_comparable")


async def test_inventory_violation_is_emitted_and_fails_the_run(arguments, monkeypatch):
    violation = {"reason": "scope_unlisted", "table": "trading_state"}

    async def inventory(session, *, scopes, policy_without_cell):
        return InventoryResult("not_comparable", (violation,))

    monkeypatch.setattr(command, "reverse_inventory", inventory)
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: Session())
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: AsyncMock(return_value=comparison()))
    output = io.StringIO()
    assert await command.run(arguments, output=output,
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0] == {"kind": "inventory", "status": "not_comparable", "violations": [violation],
                       "provenance": rows[0]["provenance"]}
    assert rows[-1]["inventory_status"] == "not_comparable"


def test_cutover_separate_manifest_actual_clock(arguments, tmp_path):
    path = tmp_path / "cutover.json"
    path.write_text(json.dumps({"mode": "cutover", "host": "db.example", "port": 6543,
                                "database": "live", "user": "reader", "run_id": "cutover"}))
    plan = validate_connection(mode="cutover", dsn="postgresql://reader:p@db.example:6543/live",
                               manifest_path=None, cutover_manifest_path=path, run_id="cutover",
                               authorize_cutover_read=True, wall_clock_ms=123456)
    assert plan.now_ms == 123456


async def test_missing_policy_is_not_dropped(arguments, monkeypatch):
    session = Session()
    original = session.execute

    async def execute(query, params=None):
        if "FROM public.capital_policy_heads" in str(query):
            return SimpleNamespace(first=lambda: None)
        return await original(query, params)

    session.execute = execute
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: session)
    comparator = AsyncMock()
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: comparator)
    output = io.StringIO()
    assert await command.run(arguments, output=output,
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()][1:]
    assert [row["status"] for row in rows[:-1]] == ["not_comparable", "not_comparable"]
    assert all(row["reason"] == "policy_missing" and row["evidence"] for row in rows[:-1])
    comparator.assert_not_called()


async def test_scope_error_rolls_back_savepoint_and_continues(arguments, monkeypatch):
    session = Session()
    savepoints = []

    async def nested():
        savepoint = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        savepoints.append(savepoint)
        return savepoint

    session.begin_nested = nested
    comparator = AsyncMock(side_effect=[RuntimeError("secret SQL"), comparison()])
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: session)
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: comparator)
    output = io.StringIO()
    assert await command.run(arguments, output=output,
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == 1
    assert "secret" not in output.getvalue()
    rows = [json.loads(line) for line in output.getvalue().splitlines()][1:]
    assert [row["status"] for row in rows[:-1]] == ["error", "equal"]
    savepoints[0].rollback.assert_awaited_once()
    savepoints[1].commit.assert_awaited_once()


async def test_empty_cells_inconclusive_and_allowlist_duplicates_deduplicated(arguments, tmp_path, monkeypatch):
    parsed = command.parser().parse_args([*arguments, "--scope", f"{ACCOUNT}:ci"])
    assert len(command.scopes_from_args(parsed)) == 2
    empty = tmp_path / "empty.yaml"
    empty.write_text("cells: []\n")
    change(arguments, "--cells", str(empty))
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: Session())
    output = io.StringIO()
    assert await command.run(arguments, output=output,
                             connector=lambda plan: SimpleNamespace(dispose=AsyncMock())) == 1
    assert json.loads(output.getvalue().splitlines()[-1])["inconclusive"] is True


async def test_cleanup_failure_has_no_success_summary(arguments, monkeypatch):
    monkeypatch.setattr(command, "AsyncSession", lambda *a, **kw: Session())
    monkeypatch.setattr(command, "build_capital_comparator", lambda **kw: AsyncMock(return_value=comparison()))
    output = io.StringIO()
    assert await command.run(arguments, output=output, connector=lambda plan: SimpleNamespace(
        dispose=AsyncMock(side_effect=RuntimeError("secret")),
    )) == 3
    summaries = [row for line in output.getvalue().splitlines()
                 if (row := json.loads(line))["kind"] == "summary"]
    assert len(summaries) == 1 and summaries[0]["exit_code"] == 3


@pytest.mark.parametrize("fault", ["group_readable", "missing", "two_lines", "empty"])
async def test_dsn_file_rejection_never_connects(arguments, fault):
    dsn_file = Path(arguments[arguments.index("--dsn-file") + 1])
    if fault == "group_readable":
        dsn_file.chmod(0o640)
    elif fault == "missing":
        dsn_file.unlink()
    elif fault == "two_lines":
        dsn_file.write_text("postgresql://a@h:5432/d\npostgresql://b@h:5432/d\n")
    else:
        dsn_file.write_text("\n")
    connects = []
    output = io.StringIO()
    code = await command.run(arguments, output=output, connector=lambda plan: connects.append(plan))
    assert code == 3 and connects == []
    assert "secret" not in output.getvalue()


def test_credentials_never_travel_in_argv(arguments):
    assert not any("secret" in value for value in arguments)


def test_login_may_not_be_the_reader(arguments, tmp_path):
    dsn = Path(arguments[arguments.index("--dsn-file") + 1])
    dsn.write_text(f"postgresql://{READER_ROLE}:secret@bfx-dr-test-db:5432/copy\n")
    manifest = Path(arguments[arguments.index("--manifest") + 1])
    data = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**data, "user": READER_ROLE}))
    with pytest.raises(command.GuardRejectedError) as raised:
        validate_connection(mode="rehearsal", dsn=dsn.read_text().strip(), manifest_path=manifest,
                            cutover_manifest_path=None, run_id="test",
                            authorize_cutover_read=False, wall_clock_ms=1)
    assert raised.value.reason == "login_is_reader"


async def test_verify_refuses_a_session_that_already_is_the_reader():
    session = Session((READER_ROLE, "copy", "repeatable read", "on"))
    plan = ConnectionPlan(None, READER_ROLE, "copy", "t", 1, "rehearsal")
    with pytest.raises(GuardRejectedError) as raised:
        await verify_connection(session, plan)
    assert raised.value.reason == "login_is_reader"
    assert not any("SET LOCAL ROLE" in sql for sql in session.statements)
