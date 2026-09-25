from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


async def test_position_state_per_symbol_pk_after_upgrade(
    pg_engine, monkeypatch
) -> None:
    # Phase 2 migration b7c1d2e3f4a5 DROP/CREATEs position_state with the
    # per-symbol composite PK. Halt 1 extends that identity to the canonical
    # exchange account UUID. Drive a real alembic upgrade against the
    # testcontainer Postgres and assert the live PK matches. alembic/env.py
    # builds Settings() from DATABASE_URL, so point it at the testcontainer's
    # sync (psycopg) URL.
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect

    # pg_engine pre-creates the ORM schema via Base.metadata.create_all but leaves
    # no alembic_version stamp; drop it so the upgrade applies the full migration
    # chain cleanly against an empty DB.
    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            # The auth-schema migration creates `auth` too; drop it as well so a
            # reused session-scoped container doesn't carry it over and fail the
            # next upgrade with `relation "auth.user" already exists`.
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_ALEMBIC_INI))
    command.upgrade(cfg, "head")

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            pk = inspect(verify_conn).get_pk_constraint("position_state")[
                "constrained_columns"
            ]
    finally:
        verify_eng.dispose()

    assert set(pk) == {"exchange_account_id", "deployment_environment", "symbol"}
