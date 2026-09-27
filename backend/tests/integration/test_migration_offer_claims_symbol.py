import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_offer_claims_has_symbol_after_upgrade(pg_head_url) -> None:
    # A fresh copy of the database Alembic migrated from empty to head.
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            cols = {c["name"]: c for c in inspect(verify_conn).get_columns("offer_claims")}
    finally:
        verify_eng.dispose()

    assert "symbol" in cols
    assert cols["symbol"]["nullable"] is False
