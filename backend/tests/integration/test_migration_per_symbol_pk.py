from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


async def test_position_state_per_symbol_pk_after_upgrade(pg_head_url) -> None:
    # Phase 2 migration b7c1d2e3f4a5 DROP/CREATEs position_state with the
    # per-symbol composite PK. Halt 1 extends that identity to the canonical
    # exchange account UUID. The database is a fresh copy of a real alembic
    # upgrade from empty to head on the testcontainer Postgres; assert the live
    # PK matches.
    sync_url = pg_head_url

    from sqlalchemy import create_engine, inspect

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            pk = inspect(verify_conn).get_pk_constraint("position_state", schema="legacy_archive")[
                "constrained_columns"
            ]
    finally:
        verify_eng.dispose()

    assert set(pk) == {"exchange_account_id", "deployment_environment", "symbol"}
