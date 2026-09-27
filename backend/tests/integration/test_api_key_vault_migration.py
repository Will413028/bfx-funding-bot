import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_api_keys_table_and_fk_after_upgrade(pg_head_url) -> None:
    """Real alembic upgrade to head: assert public.api_keys exists, has the unique
    index, and a CASCADE FK to user_profiles.user_id. Do NOT import vault/profile
    models here (would trigger create_all before the migration runs)."""
    # A fresh copy of the database Alembic migrated from empty to head.
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as conn:
            insp = inspect(conn)
            tables = set(insp.get_table_names(schema="public"))
            fks = insp.get_foreign_keys("api_keys", schema="public")
            indexes = insp.get_indexes("api_keys", schema="public")
            uniques = insp.get_unique_constraints("user_profiles", schema="public")
    finally:
        verify_eng.dispose()

    assert "api_keys" in tables, f"public has {tables}"
    referring = [
        fk for fk in fks
        if fk.get("referred_table") == "user_profiles"
        and fk.get("constrained_columns") == ["user_id"]
    ]
    assert referring, f"api_keys has no FK to user_profiles; fks={fks}"
    assert referring[0].get("options", {}).get("ondelete", "").upper() == "CASCADE"
    assert any(
        idx["column_names"] == ["user_id"] and idx["unique"] for idx in indexes
    ), f"missing unique index on api_keys.user_id; indexes={indexes}"
    assert any(
        uc["column_names"] == ["user_id"] for uc in uniques
    ), f"user_profiles missing unique constraint on user_id; uniques={uniques}"
