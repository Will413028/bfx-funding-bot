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
