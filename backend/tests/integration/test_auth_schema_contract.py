"""The migrated ``auth`` schema has every column the installed Better Auth needs.

Alembic, not Better Auth, creates the auth tables. ``frontend/src/lib/auth-db-schema.json``
lists the columns Better Auth reads and writes with this app's plugins; a frontend test keeps
it equal to the installed version. A Better Auth upgrade that needs a new column therefore
fails here until a migration adds it, instead of failing sign-in in production.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

CONTRACT = Path(__file__).resolve().parents[3] / "frontend/src/lib/auth-db-schema.json"


def test_auth_schema_has_every_column_better_auth_needs(pg_head_url: str) -> None:
    required: dict[str, list[str]] = json.loads(CONTRACT.read_text())
    assert required, "empty auth schema contract"
    engine = create_engine(pg_head_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'auth'"
            )).all()
    finally:
        engine.dispose()
    present: dict[str, set[str]] = {}
    for table, column in rows:
        present.setdefault(table, set()).add(column)
    missing = {
        table: sorted(set(columns) - present.get(table, set()))
        for table, columns in required.items()
        if set(columns) - present.get(table, set())
    }
    assert not missing, f"auth columns Better Auth needs but no migration creates: {missing}"
