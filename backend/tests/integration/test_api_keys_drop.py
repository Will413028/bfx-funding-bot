"""``f486e11435c9``: the empty SP2 ``api_keys`` table is dropped, never one that holds a key.

Each test starts from ``638455da00bb`` (the head before it).

* upgrade: the table, its grants and its two foreign keys are gone;
* refusals, each leaving the table and the revision as they were: a row in it; a function
  body that reads it; a view on it (the drop is RESTRICT, so PostgreSQL refuses).

Mutation checks (one at a time; revert after each):

* drop the row-count check: ``test_a_table_that_holds_a_key_refuses`` fails (the row goes);
* drop the function-body check: ``test_a_function_that_reads_it_refuses`` fails;
* make the drop CASCADE: ``test_a_view_on_it_refuses`` fails (the view goes with it).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_BEFORE = "638455da00bb"
_DROP = "f486e11435c9"
_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_USER = "1234567890"


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _build_before(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    engine.dispose()
    alembic(url, "upgrade", _BEFORE)
    stamp_realm(url, "ci")


def _state(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        present = conn.scalar(text("SELECT to_regclass('public.api_keys') IS NOT NULL"))
        return {
            "revision": conn.scalar(text("SELECT version_num FROM alembic_version")),
            "table": present,
            "rows": conn.scalar(text("SELECT count(*) FROM public.api_keys")) if present else None,
        }


def _refuses(engine: Engine, match: str) -> None:
    before = _state(engine)
    assert before["revision"] == _BEFORE and before["table"] is True
    with pytest.raises(Exception, match=match):
        alembic(_url(engine), "upgrade", _DROP)
    assert _state(engine) == before


@pytest.fixture
def before_db(pg_templates, pg_clone) -> Iterator[Engine]:
    engine = create_engine(pg_clone(pg_templates.template(f"api_keys_drop_{_BEFORE}",
                                                          _build_before)))
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_drops_the_empty_table(before_db) -> None:
    assert _state(before_db) == {"revision": _BEFORE, "table": True, "rows": 0}

    alembic(_url(before_db), "upgrade", _DROP)

    assert _state(before_db) == {"revision": _DROP, "table": False, "rows": None}
    with before_db.connect() as conn:
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname LIKE 'fk_api_keys_%'")) == 0
        assert conn.scalar(text(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE table_schema = 'public' AND table_name = 'api_keys'")) == 0


def test_a_table_that_holds_a_key_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts(id, venue, label, lifecycle_status) "
                          "VALUES (:a, 'bitfinex', 'x', 'active')"), {"a": _ACCOUNT})
        conn.execute(text("INSERT INTO user_profiles (user_id) VALUES (:u)"), {"u": _USER})
        conn.execute(text(
            "INSERT INTO api_keys (user_id, exchange_account_id, api_key, secret_ciphertext, "
            "secret_nonce, wrapped_dek, dek_nonce) VALUES (:u, :a, 'k', '\\x00', '\\x00', "
            "'\\x00', '\\x00')"), {"u": _USER, "a": _ACCOUNT})
    _refuses(before_db, "api_keys holds 1 rows")


def test_a_function_that_reads_it_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE FUNCTION public.count_keys() RETURNS bigint LANGUAGE plpgsql "
            "AS $$ BEGIN RETURN (SELECT count(*) FROM public.api_keys); END $$")
    _refuses(before_db, "1 function bodies read api_keys")


def test_a_view_on_it_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql("CREATE VIEW public.key_labels AS SELECT label FROM public.api_keys")
    _refuses(before_db, "other objects depend on it")
