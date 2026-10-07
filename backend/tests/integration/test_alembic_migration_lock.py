from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


async def test_do_run_migrations_sets_timeouts_and_takes_lock(pg_engine, monkeypatch) -> None:
    # Drive a real alembic upgrade (which calls env.py's do_run_migrations against
    # a live connection) and assert it SET the timeouts and acquired+released the
    # migration advisory lock. alembic/env.py builds Settings() from DATABASE_URL,
    # so point it at the testcontainer's sync (psycopg) URL.
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import Connection

    # pg_engine pre-creates the ORM schema via Base.metadata.create_all but leaves
    # no alembic_version stamp; drop it so the upgrade applies the full migration
    # chain cleanly against an empty DB.
    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS legacy_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    # Spy on exec_driver_sql across all connections during the upgrade so we
    # capture the lock/timeout SQL emitted by do_run_migrations.
    captured: list[str] = []
    real_exec = Connection.exec_driver_sql

    def spy(self, statement, *a, **k):
        captured.append(str(statement))
        return real_exec(self, statement, *a, **k)

    monkeypatch.setattr(Connection, "exec_driver_sql", spy)

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["configure_logger"] = False  # in-process: keep pytest's logging
    command.upgrade(cfg, "head")

    joined = " | ".join(captured)
    assert "lock_timeout" in joined
    assert "statement_timeout" in joined
    assert "pg_try_advisory_lock" in joined
    assert "pg_advisory_unlock" in joined

    # Migrations actually applied: alembic_version stamped.
    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            version = verify_conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
            assert version is not None
    finally:
        verify_eng.dispose()


async def test_migration_is_atomic_on_failure(pg_engine, monkeypatch) -> None:
    """A mid-migration failure must leave NO partial schema and NO version stamp.

    This pins the atomicity invariant that the AUTOCOMMIT-during-setup bug broke:
    when the connection stays in AUTOCOMMIT, alembic's ``begin_transaction()`` is a
    no-op and each DDL commits individually, so a failure orphans tables and stamps
    a partial revision. The fix restores a transactional isolation level before
    running migrations, so DDL emitted before a failure is ROLLED BACK.

    We inject the failure by wrapping ``MigrationContext.run_migrations`` to emit a
    real DDL statement (a sentinel table) on the migration's own connection — i.e.
    INSIDE alembic's migration transaction — and then raise. After the upgrade
    fails we assert from a fresh session that (a) the sentinel table is absent and
    (b) ``alembic_version`` was not stamped.
    """
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, text

    # Start from a clean, empty public schema (no alembic_version stamp).
    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS legacy_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.runtime.migration import MigrationContext

    class _InjectedFailureError(RuntimeError):
        pass

    def failing_run(self, **kw):
        # Emit DDL on the migration's own connection (inside alembic's
        # begin_transaction() txn), then raise to simulate a mid-migration
        # crash. If the migration is atomic this DDL must NOT survive.
        self.connection.exec_driver_sql("CREATE TABLE _atomicity_sentinel (id int)")
        raise _InjectedFailureError("injected mid-migration failure")

    monkeypatch.setattr(MigrationContext, "run_migrations", failing_run)

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["configure_logger"] = False  # in-process: keep pytest's logging
    with pytest.raises(_InjectedFailureError):
        command.upgrade(cfg, "head")

    # Drop the monkeypatch so the verification engine is unaffected.
    monkeypatch.undo()

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            # (a) Sentinel DDL was rolled back: table must not exist.
            sentinel = verify_conn.execute(
                text("SELECT to_regclass('public._atomicity_sentinel')")
            ).scalar()
            assert sentinel is None, (
                "sentinel table survived a failed migration -> DDL was NOT rolled "
                "back (migration ran non-atomically under AUTOCOMMIT)"
            )
            # (b) No partial version stamp: alembic_version should not have been
            # created/stamped by the failed run.
            av_exists = verify_conn.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar()
            version = (
                verify_conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                if av_exists is not None
                else None
            )
            assert version is None, (
                f"alembic_version was stamped to {version!r} after a failed "
                "migration -> partial/non-atomic application"
            )
    finally:
        verify_eng.dispose()
