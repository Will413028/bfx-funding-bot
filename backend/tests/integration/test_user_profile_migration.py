import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_auth_schema_and_user_profile_after_upgrade(pg_head_url) -> None:
    """Drive a real alembic upgrade from empty to head; assert ``public.user_profiles``
    exists and the Better Auth ``auth`` schema does not: ``f836fdee9bf3`` (9d-7) dropped it
    and with it the cross-schema FK (user_profiles.user_id -> auth.user.id), while the vault
    tables keep their FKs to ``user_profiles``.
    """
    # A fresh copy of the database Alembic migrated from empty to head.
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            insp = inspect(verify_conn)
            schemas = set(insp.get_schema_names())
            public_tables = set(insp.get_table_names(schema="public"))
            fks = insp.get_foreign_keys("user_profiles", schema="public")
            vault_fks = {
                fk["name"]: fk["referred_table"]
                for table in ("api_keys", "user_configs")
                for fk in insp.get_foreign_keys(table, schema="public")
            }
    finally:
        verify_eng.dispose()

    assert "auth" not in schemas, f"auth schema still present; schemas={schemas}"
    assert "user_profiles" in public_tables, (
        f"public.user_profiles missing; public has {public_tables}"
    )
    assert not [fk for fk in fks if fk.get("referred_schema") == "auth"], (
        f"user_profiles still has an FK into auth; foreign_keys={fks}"
    )
    assert vault_fks.get("fk_api_keys_user_profile") == "user_profiles"
    assert vault_fks.get("fk_user_configs_user_profile") == "user_profiles"
