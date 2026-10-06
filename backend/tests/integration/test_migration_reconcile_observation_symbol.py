import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_reconcile_observation_has_symbol_after_upgrade(pg_head_url) -> None:
    """Drive a real alembic upgrade against testcontainer Postgres; assert the
    reconcile_observation.symbol column exists and is NOT NULL after head."""
    # A fresh copy of the database Alembic migrated from empty to head (the table is
    # archived since c2d3e4f5a6b7, unchanged).
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            cols = {c["name"]: c for c in inspect(verify_conn).get_columns("reconcile_observation", schema="legacy_archive")}
    finally:
        verify_eng.dispose()

    assert "symbol" in cols
    assert cols["symbol"]["nullable"] is False
