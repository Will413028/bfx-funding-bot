"""Ledger restore verification (restore_drill.py --restore-test) on real PostgreSQL clones.

SIMULATION NOTE: pgBackRest + R2 cannot run here. The restore point is simulated by cloning
one seeded template twice: the "restored" clone stays at the restore point, the "production"
clone keeps writing afterwards. Everything after that is the production code path: the bounds
script and the bounded COPY script through `psql` (the drill's own streaming runner), the
comparison, the drill's ledger bootstrap grants, and ledger_boot_check.py fed to `python -`.

Mutations, each applied alone to deploy/vm/pgbackrest/ledger_digest.py and reverted:

* drop the upper bound of a rule (``where`` -> ``TRUE``):
  ``test_production_ahead_of_the_restore_point_matches`` fails (ledger_digest_mismatch).
* drop the pending-attempt exclusion of transport_outcome_journal: the same test fails.
* compare resolutions with ``<=`` instead of ``<``:
  ``test_resolutions_naming_the_restored_latest_accepted_observation_are_not_compared`` fails.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from tests import pg_local

from .test_ledger_schema_roles import _A, _B, _D2, _O, _P, _QID, _build, _seed

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
PGBACKREST = ROOT / "deploy/vm/pgbackrest"

_D3 = "ledger-decision-3"
_T2 = "00000000-0000-0000-0000-00000000c002"  # attempt without an outcome at the restore point
_T3 = "00000000-0000-0000-0000-00000000c003"
_Q2 = "00000000-0000-0000-0000-00000000c011"
_O2 = "00000000-0000-0000-0000-00000000c012"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


drill = _load("ledger_restore_verification_drill", PGBACKREST / "restore_drill.py")
ledger = drill._ledger


def _decision_sql(decision_id: str) -> str:
    return (
        "INSERT INTO execution_decisions(decision_id, account_id, exchange_account_id, "
        "deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id, outcome, "
        "signal_rate, amount_usdt, duration_days, model_evidence, safety_result, execution_policy, "
        "service_version, config_hash, occurred_at_ms, recorded_at_ms) "
        f"VALUES ('{decision_id}', 'account', '{_A}', 'ci', 'r', 'cell', 'fUST', '{decision_id}', "
        "'submitted', 0, 1, 2, '{}', '{}', 'policy', 'test', 'hash', 1, 1)"
    )


def _attempt_sql(attempt_id: str, decision_id: str, seq: int) -> str:
    return (
        "INSERT INTO submission_attempt_journal(attempt_id, execution_decision_id, "
        "exchange_account_id, deployment_environment, symbol, cell_id, attempt_seq, "
        "normalized_payload, payload_sha256, basis_id, policy_revision_id, "
        "authorization_evidence, started_at_ms) "
        f"VALUES ('{attempt_id}', '{decision_id}', '{_A}', 'ci', 'fUST', 'cell', {seq}, "
        f"'{{\"amount\": \"1.10\"}}', 'hash', '{_B}', '{_P}', '{{}}', {seq + 3})"
    )


def _outcome_sql(attempt_id: str) -> str:
    return (
        "INSERT INTO transport_outcome_journal(attempt_id, kind, venue_offer_id, "
        f"completed_at_ms, evidence) VALUES ('{attempt_id}', 'ack', 'offer-1', 9, '{{}}')"
    )


def _template(url: str) -> None:
    """The ledger at the restore point: one accepted observation + basis, every table filled,
    one attempt still without an outcome, the epoch switched to the ledger."""
    _build(url)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _seed(conn)
            conn.exec_driver_sql(_decision_sql(_D3))
            conn.exec_driver_sql("UPDATE capital_command_clock SET revision = 1")  # the opening
            conn.exec_driver_sql(_attempt_sql(_T2, _D2, 2))
            conn.exec_driver_sql(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, "
                "reason) VALUES (2, 'ledger', 2, 'test', 'switch')"
            )
    finally:
        engine.dispose()


def _production_moves_on(url: str) -> None:
    """Writes committed after the restore point: none may be compared."""
    with psycopg.connect(_libpq(url), autocommit=True) as conn:
        conn.execute(_outcome_sql(_T2))  # the pending attempt's outcome arrives
        conn.execute(_attempt_sql(_T3, _D3, 3))
        conn.execute(_outcome_sql(_T3))
        conn.execute(
            "INSERT INTO ledger_observation_query (query_id, exchange_account_id, "
            "deployment_environment, query_revision, started_at_ms, start_revision) "
            f"VALUES ('{_Q2}', '{_A}', 'ci', 2, 10, 1)"
        )
        conn.execute(
            "INSERT INTO ledger_observation (id, query_id, exchange_account_id, "
            "deployment_environment, schema_version, query_finished_at_ms, "
            "confirmation_finished_at_ms, accept_revision, wallets_complete, offers_complete, "
            "credits_complete, loans_complete, offer_history_complete, credit_history_complete, "
            "trades_complete, first_digest, confirmation_digest, accepted, evidence) VALUES "
            f"('{_O2}', '{_Q2}', '{_A}', 'ci', 1, 11, 12, 1, true, true, true, true, true, true, "
            "true, 'd', 'd', false, '{}')"
        )
        conn.execute(
            "INSERT INTO ledger_observation_wallet(observation_id, wallet_type, currency, "
            f"available, balance) VALUES ('{_O2}', 'funding', 'UST', 11, 11)"
        )
        conn.execute("UPDATE capital_command_clock SET revision = 2")
        conn.execute(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, "
            "reason) VALUES (3, 'ledger', 3, 'test', 'later')"
        )


def _libpq(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _psql(url: str) -> tuple[str, ...]:
    pg_bin = pg_local.find_pg_bin()
    assert pg_bin is not None
    return (str(pg_bin / "psql"), _libpq(url), "-X", "-qAt", "-F", "\t", "-v", "ON_ERROR_STOP=1")


def _bounds(url: str) -> Any:
    completed = subprocess.run(_psql(url), input=ledger.bounds_script(), capture_output=True,
                               text=True, check=False, timeout=120)
    assert completed.returncode == 0, completed.stderr
    return ledger.parse_bounds(completed.stdout)


def _digest(url: str, bounds: Any, *, restored: bool) -> Any:
    digest = ledger.StreamDigest(ledger.compared_tables(bounds))
    status = drill._stream_command(_psql(url), input_text=ledger.digest_script(bounds, restored=restored, timeout_ms=60_000),
                                   timeout=120, consume=digest)
    assert status == 0
    return digest


def _verify(restored_url: str, production_url: str) -> dict[str, Any]:
    bounds = _bounds(restored_url)
    return ledger.compare(bounds, _digest(restored_url, bounds, restored=True),
                          _digest(production_url, bounds, restored=False))


def _superuser(url: str, *statements: str) -> None:
    """Change the restored copy behind the triggers (what a corrupt restore would look like)."""
    with psycopg.connect(_libpq(url), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def clusters(pg_templates, pg_clone) -> tuple[str, str]:
    template = pg_templates.template("ledger_restore_verification", _template)
    restored, production = pg_clone(template), pg_clone(template)
    _production_moves_on(production)
    return restored, production


def test_production_ahead_of_the_restore_point_matches(clusters) -> None:
    restored, production = clusters
    summary = _verify(restored, production)
    tables = summary["tables"]
    assert set(tables) == set(ledger.RULES) and summary["absent"] == []
    # Each table holds its restored rows only, although production holds more of most.
    assert tables["ledger_observation_query"]["count"] == 1
    assert tables["ledger_observation"]["count"] == 1
    assert tables["ledger_observation_wallet"]["count"] == 1
    assert tables["submission_attempt_journal"]["count"] == 2
    assert tables["transport_outcome_journal"]["count"] == 1  # T2's later outcome excluded
    assert tables["capital_authority_epoch"]["count"] == 2
    assert all(entry["count"] == entry["restored_total"] for name, entry in tables.items()
               if name not in ledger.PARTIAL_TABLES)
    [scope] = summary["scopes"]
    assert (scope["exchange_account_id"], scope["deployment_environment"]) == (_A, "ci")
    assert (scope["clock_revision"], scope["production_clock_revision"]) == (1, 2)
    assert (scope["query_revision"], scope["attempt_seq"], scope["opened_revision"]) == (1, 2, 1)
    assert summary["pending_attempts"] == 1 and summary["epoch_seq"] == 2
    assert summary["rows_compared"] == sum(entry["count"] for entry in tables.values())
    assert set(summary["mutable"]) == set(ledger.MUTABLE_TABLES)
    # Production really holds more: the bound is what makes the two comparable.
    with psycopg.connect(_libpq(production)) as conn:
        assert conn.execute("SELECT count(*) FROM transport_outcome_journal").fetchone() == (3,)
        assert conn.execute("SELECT count(*) FROM ledger_observation_wallet").fetchone() == (2,)


def test_resolutions_naming_the_restored_latest_accepted_observation_are_not_compared(
    clusters,
) -> None:
    restored, production = clusters
    summary = _verify(restored, production)
    # The seeded resolution names the restored latest accepted observation: not compared...
    assert summary["tables"]["execution_resolution_journal"]["count"] == 0
    assert summary["tables"]["execution_resolution_journal"]["restored_total"] == 1
    # ...so production may hold a different set of them (an operator may still add one).
    _superuser(production, "DELETE FROM execution_resolution_journal")
    assert _verify(restored, production)["tables"]["execution_resolution_journal"]["count"] == 0


@pytest.mark.parametrize(("statement", "table"), [
    ("UPDATE ledger_observation_wallet SET balance = 10.0", "ledger_observation_wallet"),
    ("DELETE FROM ledger_observation_trade", "ledger_observation_trade"),
    ("UPDATE submission_attempt_journal SET payload_sha256 = 'other' WHERE attempt_seq = 1",
     "submission_attempt_journal"),
    ("DELETE FROM accepted_capital_basis_cell", "accepted_capital_basis_cell"),
    ("UPDATE capital_authority_epoch SET reason = 'other' WHERE epoch_seq = 1",
     "capital_authority_epoch"),
])
def test_a_restored_row_that_differs_within_the_bound_is_a_mismatch(
    clusters, statement: str, table: str,
) -> None:
    restored, production = clusters
    _superuser(restored, statement)
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_digest_mismatch$") as exc:
        _verify(restored, production)
    assert exc.value.detail == (table,)


def test_a_production_row_inside_the_bound_that_the_restore_lacks_is_a_mismatch(clusters) -> None:
    restored, production = clusters
    with psycopg.connect(_libpq(production), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO ledger_observation_wallet(observation_id, wallet_type, currency, "
            f"available, balance) VALUES ('{_O}', 'funding', 'BTC', 1, 1)"
        )
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_digest_mismatch$") as exc:
        _verify(restored, production)
    assert exc.value.detail == ("ledger_observation_wallet",)


def test_a_restored_clock_ahead_of_production_fails(clusters) -> None:
    restored, production = clusters
    _superuser(restored, "UPDATE capital_command_clock SET revision = 9")
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_ahead_of_production$"):
        _verify(restored, production)


@pytest.mark.parametrize(("statement", "check"), [
    ("UPDATE capital_command_clock SET revision = 0", "clock_behind"),
    (f"UPDATE venue_offer_mirror SET last_accepted_observation_id = '{_QID}'",
     "venue_offer_mirror_orphans"),
])
def test_an_inconsistent_restored_copy_fails(clusters, statement: str, check: str) -> None:
    restored, production = clusters
    _superuser(restored, statement)
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_restored_inconsistent$") as exc:
        _verify(restored, production)
    assert exc.value.detail == (check,)


def test_a_bound_rule_that_drops_restored_rows_fails_closed(
    clusters, monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored, production = clusters
    rule = ledger.RULES["ledger_observation_wallet"]
    monkeypatch.setitem(ledger.RULES, "ledger_observation_wallet",
                        ledger.Rule(rule.join, "q.query_revision < b.q_obs", rule.bound))
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_bound_invalid$") as exc:
        _verify(restored, production)
    assert exc.value.detail == ("ledger_observation_wallet",)


def test_the_epoch_must_be_the_ledger(clusters) -> None:
    restored, production = clusters
    _superuser(restored, "DELETE FROM capital_authority_epoch WHERE epoch_seq = 2")
    with pytest.raises(ledger.LedgerVerificationError, match=r"^ledger_authority_not_ledger$"):
        _verify(restored, production)


# --------------------------------------------------------------------------- boot check


def _verifier_url(restored: str) -> str:
    """The drill's own ephemeral role (its bootstrap SQL), as the boot check connects."""
    resources = drill.build_restore_resources(
        backup_label="20261001-031700F", target_time=None, run_id=drill._new_run_id(),
        database_name=make_url(restored).database)
    password = drill._new_password()

    def runner(command: tuple[str, ...], *, input_text: str | None = None,
               timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        with psycopg.connect(_libpq(restored), autocommit=True) as conn:
            conn.execute(input_text)
        return subprocess.CompletedProcess(command, 0, "", "")

    bootstrap = drill.RestoreDrill(command_runner=runner)
    bootstrap._deadline = drill.time.monotonic() + 600
    bootstrap._bootstrap_role(resources, password, ledger=True)
    return make_url(restored).set(
        drivername="postgresql+asyncpg", username=resources.verify_role, password=password,
    ).render_as_string(hide_password=False)


def _boot_check(database_url: str) -> subprocess.CompletedProcess[str]:
    """Exactly how the drill runs it: the script on stdin to `python -`, cwd = the app root."""
    return subprocess.run(
        [sys.executable, "-"], input=(PGBACKREST / "ledger_boot_check.py").read_text(),
        cwd=ROOT / "backend", env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True, text=True, check=False, timeout=300,
    )


def test_boot_check_accepts_the_restored_copy_with_the_drill_grants_only(clusters) -> None:
    restored, _ = clusters
    completed = _boot_check(_verifier_url(restored))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    boot = ledger.parse_boot(completed.stdout, _bounds(restored))
    assert (boot["authority"], boot["realm"]) == ("ledger", "ci")
    [scope] = boot["scopes"]
    assert scope["basis_id"] == _B
    assert [(read["symbol"], read["cell_id"], read["basis_id"]) for read in scope["reads"]] == [
        ("fUST", "cell", _B)]


def test_boot_check_refuses_a_restored_copy_that_is_not_on_the_ledger(clusters) -> None:
    restored, _ = clusters
    _superuser(restored, "DELETE FROM capital_authority_epoch WHERE epoch_seq = 2")
    completed = _boot_check(_verifier_url(restored))
    assert completed.returncode == 3
    assert json.loads(completed.stdout.splitlines()[-1]) == {"error": "boot_authority_not_ledger"}
    assert ledger.boot_failure_code(completed.stdout) == "boot_authority_not_ledger"


def test_boot_check_role_cannot_write(clusters) -> None:
    restored, _ = clusters
    url = make_url(_verifier_url(restored)).set(drivername="postgresql")
    with (psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn,
          pytest.raises(psycopg.errors.ReadOnlySqlTransaction)):
        conn.execute("UPDATE capital_command_clock SET revision = revision")


def test_the_server_ends_a_read_that_outlives_its_budget(clusters) -> None:
    """A killed client cannot leave production's snapshot running past the drill's budget."""
    restored, production = clusters
    script = ledger.digest_script(_bounds(restored), restored=False, timeout_ms=300)
    stalled = script.replace("SET LOCAL lock_timeout = '10s';",
                             "SET LOCAL lock_timeout = '10s';\nSELECT pg_sleep(5);", 1)
    completed = subprocess.run(_psql(production), input=stalled, capture_output=True, text=True,
                               check=False, timeout=60)
    assert completed.returncode != 0
    assert "timeout" in completed.stderr
