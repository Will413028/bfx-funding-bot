import pytest

from tests.integration.test_migration_per_symbol_pk import _ALEMBIC_INI

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_auth_schema_and_user_profile_after_upgrade(pg_engine, monkeypatch) -> None:
    """Drive a real alembic upgrade against testcontainer Postgres; assert the
    Better Auth ``auth`` schema, ``public.user_profiles``, and the cross-schema
    FK (user_profiles.user_id -> auth.user.id) all exist after head.

    NOTE: do NOT import the ``UserProfile`` model here — importing it registers
    it on ``Base.metadata`` and the session-scoped ``pg_engine`` fixture's
    ``create_all`` would then try to build ``user_profiles`` (FK to ``auth.user``)
    before this migration runs, breaking every test that uses ``pg_engine``.
    """
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect

    # Reset BOTH schemas: the migration creates `auth` too, and the session-scoped
    # container is reused across migration tests — leaving `auth` behind would make
    # the next `command.upgrade(head)` fail with `relation "auth.user" already exists`.
    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.config import Config

    from alembic import command
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            insp = inspect(verify_conn)
            auth_tables = set(insp.get_table_names(schema="auth"))
            public_tables = set(insp.get_table_names(schema="public"))
            fks = insp.get_foreign_keys("user_profiles", schema="public")
    finally:
        verify_eng.dispose()

    assert "user" in auth_tables, f"auth.user missing; auth has {auth_tables}"
    assert "user_profiles" in public_tables, (
        f"public.user_profiles missing; public has {public_tables}"
    )

    referring = [
        fk
        for fk in fks
        if fk.get("referred_table") == "user"
        and fk.get("referred_schema") == "auth"
    ]
    assert referring, (
        f"user_profiles has no FK to auth.user; foreign_keys={fks}"
    )
    assert referring[0]["constrained_columns"] == ["user_id"]
