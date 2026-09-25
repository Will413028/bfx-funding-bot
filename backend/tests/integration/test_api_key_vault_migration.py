import pytest

from tests.integration.test_migration_per_symbol_pk import _ALEMBIC_INI

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_api_keys_table_and_fk_after_upgrade(pg_engine, monkeypatch) -> None:
    """Real alembic upgrade to head: assert public.api_keys exists, has the unique
    index, and a CASCADE FK to user_profiles.user_id. Do NOT import vault/profile
    models here (would trigger create_all before the migration runs)."""
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect

    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.config import Config

    from alembic import command
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

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
