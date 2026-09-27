import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_auth_schema_and_user_profile_after_upgrade(pg_head_url) -> None:
    """Drive a real alembic upgrade against testcontainer Postgres; assert the
    Better Auth ``auth`` schema, ``public.user_profiles``, and the cross-schema
    FK (user_profiles.user_id -> auth.user.id) all exist after head.

    NOTE: do NOT import the ``UserProfile`` model here — importing it registers
    it on ``Base.metadata`` and the session-scoped ``pg_engine`` fixture's
    ``create_all`` would then try to build ``user_profiles`` (FK to ``auth.user``)
    before this migration runs, breaking every test that uses ``pg_engine``.
    """
    # A fresh copy of the database Alembic migrated from empty to head.
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

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
