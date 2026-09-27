"""The deployments ledger on real PostgreSQL, written the way bfx-deploy writes it.

Runs the actual migrations, then drives PsqlLedger through `docker exec <pg>
psql` against the test container -- the same argv, quoting and \\if logic as on
the VM -- and proves the table is append-only and read-only to runtime roles.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
REVISION = "c74d45a54e46"          # creates the ledger
RETIRED = "1f6392809120"           # change_class nullable, no CHECK
REV = "b" * 40
ATTEMPT = "0b8f7c5e-5d59-4a4c-9b58-6f0a1c2d3e4f"
DIGEST_B, DIGEST_F = "sha256:" + "3" * 64, "sha256:" + "4" * 64


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx = _load("vm_ops_bfx_deploy_for_ledger", ROOT / "deploy/vm/ops/bfx_deploy.py")


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(["uv", "run", "alembic", *args], cwd=BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _entry(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "attempt_id": str(__import__("uuid").uuid4()),
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:04:00+00:00",
        "source_revision": REV, "backend_digest": DIGEST_B, "frontend_digest": DIGEST_F,
        "migrations_applied": False, "outcome": "deployed",
        "detail": "ok", **overrides,
    }
    return bfx.LedgerEntry(**values)


@pytest.fixture
def ledger_db(pg_container: Any) -> Any:
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        # Worst case on the VM: default privileges already hand runtime roles ALL.
        for role in ("bfx_bot", "bfx_webapi", "bfx_webauth"):
            conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 f"rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
    ledger = bfx.PsqlLedger(bfx.subprocess_runner, container=pg_container.get_wrapped_container().id,
                            db_user=pg_container.username, db_name=pg_container.dbname)
    try:
        yield url, engine, ledger
    finally:
        with engine.begin() as conn:
            for role in ("bfx_bot", "bfx_webapi", "bfx_webauth"):
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
        engine.dispose()


def test_ledger_reads_absent_before_the_migration_then_appends_and_reads_back(ledger_db: Any) -> None:
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", "a7f3c1d9e204")
    assert ledger.read() == bfx.LedgerView(exists=False, last_attempt=None, last_success=None)
    _alembic(url, "upgrade", REVISION)
    assert ledger.read() == bfx.LedgerView(exists=True, last_attempt=None, last_success=None)
    _alembic(url, "upgrade", "head")

    hostile = "unhealthy:x'; DROP TABLE deployments; -- \\gset :'detail'"
    first = ledger.append(_entry())
    second = ledger.append(_entry(outcome="rolled_back", migrations_applied=False, detail=hostile))
    assert second == first + 1
    view = ledger.read()
    assert view.last_attempt is not None and view.last_success is not None
    assert (view.last_attempt.id, view.last_attempt.outcome) == (second, "rolled_back")
    assert (view.last_success.id, view.last_success.backend_digest) == (first, DIGEST_B)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT detail FROM deployments WHERE id = :id"), {"id": second}) == hostile
        assert conn.scalar(text("SELECT ci_run FROM deployments WHERE id = :id"), {"id": first}) is None
        # The retired column is gone.
        assert conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE "
                                "table_name = 'deployments' AND column_name = 'change_class'")) == 0


def _old_tool_insert(conn: Any, attempt: str, outcome: str, klass: str) -> None:
    """A row the way the previous bfx-deploy writes it: change_class included."""
    finished = "NULL" if outcome == "started" else "'2026-09-25T10:04:00+00:00'"
    conn.exec_driver_sql(
        "INSERT INTO deployments (attempt_id, started_at, finished_at, source_revision, backend_digest, "
        "frontend_digest, change_class, migrations_applied, outcome, detail) VALUES "
        f"('{attempt}', '2026-09-25T10:00:00+00:00', {finished}, '{REV}', '{DIGEST_B}', '{DIGEST_F}', "
        f"'{klass}', true, '{outcome}', 'class={klass}(rules_unavailable)')")


def test_previous_tool_can_deploy_the_release_that_retires_change_class(ledger_db: Any) -> None:
    """Rollout: the old bfx-deploy migrates, then writes both rows with a class."""
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", REVISION)
    earlier = str(__import__("uuid").uuid4())
    with engine.begin() as conn:
        _old_tool_insert(conn, earlier, "deployed", "standard")
    _alembic(url, "upgrade", RETIRED)
    with engine.begin() as conn:                      # the old tool, after the migration ran
        _old_tool_insert(conn, ATTEMPT, "started", "material")
        _old_tool_insert(conn, ATTEMPT, "deployed", "material")
    ledger.append(_entry())                           # then the new tool, next release
    view = ledger.read()
    assert view.last_success is not None and view.last_success.attempt_id != ATTEMPT
    with engine.connect() as conn:
        classes = [row[0] for row in conn.execute(text("SELECT change_class FROM deployments ORDER BY id"))]
    assert classes == ["standard", "material", "material", None]
    # The follow-up release drops the column; each class stays in its row's detail.
    _alembic(url, "upgrade", "head")
    ledger.append(_entry(attempt_id=ATTEMPT.replace("0b8f", "1b8f"), outcome="started", finished_at=None))
    ledger.append(_entry(attempt_id=ATTEMPT.replace("0b8f", "1b8f")))
    with engine.connect() as conn:
        details = [row[0] for row in conn.execute(text("SELECT detail FROM deployments ORDER BY id"))]
    assert [d.split("(")[0] for d in details[:3]] == ["class=standard", "class=material", "class=material"]
    assert len(details) == 6


@pytest.mark.parametrize("statement", [
    "UPDATE deployments SET outcome = 'deployed'",
    "UPDATE deployments SET detail = 'rewritten'",
    "DELETE FROM deployments",
    "TRUNCATE deployments",
])
def test_ledger_rejects_every_mutation_even_for_the_owner(ledger_db: Any, statement: str) -> None:
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", "head")
    ledger.append(_entry(outcome="failed"))
    with engine.begin() as conn, pytest.raises(Exception, match="append-only"):
        conn.exec_driver_sql(statement)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM deployments")) == 1
        assert conn.scalar(text("SELECT outcome FROM deployments")) == "failed"


@pytest.mark.parametrize("overrides", [
    {"source_revision": "main"},
    {"backend_digest": "sha256:abc"},
    {"frontend_digest": "ghcr.io/x:main"},
    {"outcome": "no_change"},
    {"finished_at": "2026-09-25T09:00:00+00:00"},
    {"finished_at": None},                      # only a started row has no finish
    {"outcome": "started"},                     # ... and a started row has none
    {"attempt_id": "not-a-uuid"},
    {"detail": "x" * 2001},
])
def test_ledger_constraints_reject_malformed_rows(ledger_db: Any, overrides: dict[str, Any]) -> None:
    url, _, ledger = ledger_db
    _alembic(url, "upgrade", "head")
    with pytest.raises(bfx.CommandError, match="ledger_psql_exit"):
        ledger.append(_entry(**overrides))
    assert ledger.read().last_attempt is None


def test_attempt_has_one_started_row_first_and_one_matching_terminal_row(ledger_db: Any) -> None:
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", "head")
    started = _entry(attempt_id=ATTEMPT, outcome="started", finished_at=None, detail="deploy")
    ledger.append(started)
    view = ledger.read()
    assert view.last_attempt is not None and view.last_success is None
    assert (view.last_attempt.outcome, view.last_attempt.attempt_id) == ("started", ATTEMPT)
    with pytest.raises(bfx.CommandError):                      # a second started row
        ledger.append(started)
    for mismatch in ({"backend_digest": "sha256:" + "9" * 64},
                     {"source_revision": "c" * 40}, {"started_at": "2026-09-25T10:01:00+00:00"}):
        with pytest.raises(bfx.CommandError):                  # finishing as another release
            ledger.append(_entry(attempt_id=ATTEMPT, **mismatch))
    finished = ledger.append(_entry(attempt_id=ATTEMPT))
    view = ledger.read()
    assert view.last_success is not None and view.last_success.id == finished
    assert view.last_success.attempt_id == ATTEMPT
    with pytest.raises(bfx.CommandError):                      # a second terminal row
        ledger.append(_entry(attempt_id=ATTEMPT, outcome="failed"))
    closed = str(__import__("uuid").uuid4())
    ledger.append(_entry(attempt_id=closed, outcome="failed"))  # terminal row alone is fine
    with pytest.raises(bfx.CommandError):                      # but never a started row after it
        ledger.append(_entry(attempt_id=closed, outcome="started", finished_at=None))
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM deployments")) == 3


def test_runtime_roles_can_only_read_the_ledger(ledger_db: Any) -> None:
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", "head")
    ledger.append(_entry())
    with engine.connect() as conn:
        for role in ("bfx_bot", "bfx_webapi"):
            assert conn.scalar(text("SELECT has_table_privilege(:r, 'deployments', 'SELECT')"), {"r": role})
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not conn.scalar(text("SELECT has_table_privilege(:r, 'deployments', :p)"),
                                       {"r": role, "p": privilege}), (role, privilege)
        for privilege in ("SELECT", "INSERT"):
            assert not conn.scalar(text("SELECT has_table_privilege('bfx_webauth', 'deployments', :p)"),
                                   {"p": privilege})
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(
            "INSERT INTO deployments (attempt_id, started_at, finished_at, source_revision, backend_digest, "
            f"frontend_digest, migrations_applied, outcome, detail) VALUES "
            f"('{ATTEMPT}', now(), now(), "
            f"'{REV}', '{DIGEST_B}', '{DIGEST_F}', false, 'deployed', '')")


def test_migration_is_reversible_and_leaves_no_drift(ledger_db: Any) -> None:
    url, engine, ledger = ledger_db
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    # Back across the retirement: the empty ledger downgrades, all the way.
    _alembic(url, "downgrade", "a7f3c1d9e204")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.deployments')")) is None
        assert conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname = 'reject_deployment_mutation'")) == 0
    _alembic(url, "upgrade", "head")
    assert ledger.read().last_attempt is None
    # Classless rows: the downgrade refuses rather than invent a class that never
    # happened in an append-only ledger.
    ledger.append(_entry(attempt_id=ATTEMPT, outcome="started", finished_at=None))
    ledger.append(_entry(attempt_id=ATTEMPT))
    result = subprocess.run(["uv", "run", "alembic", "downgrade", REVISION], cwd=BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode != 0
    assert "recorded without a change class" in result.stdout + result.stderr
    # The refused run rolls back as a whole: still at head, both rows intact.
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM deployments")) == 2
    _alembic(url, "check")
    # One step back re-adds the column (NULL) and the pairing check still accepts
    # an attempt written the current way.
    _alembic(url, "downgrade", "-1")
    ledger.append(_entry(attempt_id=ATTEMPT.replace("0b8f", "2b8f"), outcome="started", finished_at=None))
    ledger.append(_entry(attempt_id=ATTEMPT.replace("0b8f", "2b8f")))
    with engine.connect() as conn:
        assert [row[0] for row in conn.execute(text("SELECT change_class FROM deployments"))] == [None] * 4
