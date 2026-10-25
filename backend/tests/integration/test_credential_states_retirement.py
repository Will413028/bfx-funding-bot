"""``c3e7a91d5b20``: credential rows are ``active`` or ``retired``, verified and named, and
``last_verify_error`` is gone, never on a database that still holds another shape.

Each test starts from ``f486e11435c9`` (the head before it).

* upgrade: a verified active and a verified retired row survive; the column, the default and
  the conditional CHECKs are gone; ``pending``, a NULL ``verified_at`` or a NULL
  ``bitfinex_user_id`` is refused by the schema itself;
* refusals, each leaving the table and the revision as they were: a ``pending`` or ``revoked``
  row; a row with a verification error; a row without ``bitfinex_user_id``; a key stored
  twice; a function body that names the column; a view on it (the drop is RESTRICT, so
  PostgreSQL refuses).

Mutation checks (one at a time; revert after each):

* drop the row check: every ``test_a_row_*_refuses`` case fails (the upgrade goes through
  or fails on a constraint after the revision changed nothing it should keep);
* drop the function-body check: ``test_a_function_that_names_it_refuses`` fails;
* drop the reused-key check: ``test_a_key_stored_twice_refuses`` fails (the constraint fails
  instead, with another message);
* keep the old lifecycle CHECK: ``test_upgrade_keeps_verified_rows_and_narrows_the_schema``
  fails (a pending row is accepted).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_BEFORE = "f486e11435c9"
_RETIRE = "c3e7a91d5b20"
_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_INSERT = text(
    "INSERT INTO exchange_account_credentials (exchange_account_id, venue, label, api_key, "
    "secret_ciphertext, secret_nonce, wrapped_dek, dek_nonce, key_version, lifecycle_status, "
    "verified_at, bitfinex_user_id{extra_columns}) VALUES (:a, 'bitfinex', 'k', :key, '\\x00', "
    "'\\x00', '\\x00', '\\x00', 1, :status, :verified_at, :user_id{extra_values})")


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _build_before(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    alembic(url, "upgrade", _BEFORE)
    stamp_realm(url, "ci")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts(id, venue, label, lifecycle_status) "
                          "VALUES (:a, 'bitfinex', 'x', 'active')"), {"a": _ACCOUNT})
    engine.dispose()


def _insert(engine: Engine, *, key: str, status: str, verified: bool = True,
            user_id: int | None = 4242, error: str | None = None) -> None:
    extra = ("", "") if error is None else (", last_verify_error", ", :error")
    statement = text(_INSERT.text.format(extra_columns=extra[0], extra_values=extra[1]))
    with engine.begin() as conn:
        conn.execute(statement, {"a": _ACCOUNT, "key": key, "status": status,
                                 "verified_at": "2026-10-25T00:00:00Z" if verified else None,
                                 "user_id": user_id, "error": error})


def _state(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "revision": conn.scalar(text("SELECT version_num FROM alembic_version")),
            "column": conn.scalar(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'exchange_account_credentials' "
                "AND column_name = 'last_verify_error'")) == 1,
            "rows": sorted(conn.execute(text(
                "SELECT api_key, lifecycle_status FROM exchange_account_credentials")).all()),
        }


def _refuses(engine: Engine, match: str) -> None:
    before = _state(engine)
    assert before["revision"] == _BEFORE and before["column"] is True
    with pytest.raises(Exception, match=match):
        alembic(_url(engine), "upgrade", _RETIRE)
    assert _state(engine) == before


@pytest.fixture
def before_db(pg_templates, pg_clone) -> Iterator[Engine]:
    engine = create_engine(pg_clone(pg_templates.template(
        f"credential_states_{_BEFORE}", _build_before)))
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_keeps_verified_rows_and_narrows_the_schema(before_db) -> None:
    _insert(before_db, key="old", status="retired")
    _insert(before_db, key="current", status="active")

    alembic(_url(before_db), "upgrade", _RETIRE)

    assert _state(before_db) == {"revision": _RETIRE, "column": False,
                                 "rows": [("current", "active"), ("old", "retired")]}
    with before_db.connect() as conn:
        columns = dict(conn.execute(text(
            "SELECT column_name, is_nullable || ':' || coalesce(column_default, '') "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'exchange_account_credentials' AND column_name IN "
            "('lifecycle_status', 'verified_at', 'bitfinex_user_id')")).all())
        assert columns == {"lifecycle_status": "NO:", "verified_at": "NO:",
                           "bitfinex_user_id": "NO:"}
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('ck_exchange_account_credentials_active_verified', "
            "'ck_exchange_account_credentials_active_user_id')")) == 0
    for status, verified, user_id in (("pending", True, 4242), ("revoked", True, 4242),
                                      ("retired", False, 4242), ("retired", True, None),
                                      ("retired", True, 0)):
        with pytest.raises(IntegrityError):
            _insert(before_db, key=f"{status}-{verified}-{user_id}", status=status,
                    verified=verified, user_id=user_id)
    with pytest.raises(IntegrityError, match="uq_exchange_account_credentials_venue_api_key"):
        _insert(before_db, key="old", status="retired")


@pytest.mark.parametrize("row, match", [
    ({"status": "pending", "verified": False}, "1 credential rows"),
    ({"status": "revoked"}, "1 credential rows"),
    ({"status": "retired", "error": "invalid_credentials"}, "1 credential rows"),
    ({"status": "retired", "user_id": None}, "1 credential rows"),
    ({"status": "retired", "verified": False}, "1 credential rows"),
])
def test_a_row_outside_the_new_shape_refuses(before_db, row, match) -> None:
    _insert(before_db, key="k", **row)
    _refuses(before_db, match)


def test_a_key_stored_twice_refuses(before_db) -> None:
    _insert(before_db, key="k", status="retired")
    _insert(before_db, key="k", status="active")
    _refuses(before_db, "1 keys stored more than once")


def test_a_function_that_names_it_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE FUNCTION public.count_errors() RETURNS bigint LANGUAGE plpgsql AS $$ BEGIN "
            "RETURN (SELECT count(last_verify_error) FROM public.exchange_account_credentials); "
            "END $$")
    _refuses(before_db, "1 function bodies name last_verify_error")


def test_a_view_on_it_refuses(before_db) -> None:
    with before_db.begin() as conn:
        conn.exec_driver_sql("CREATE VIEW public.credential_errors AS "
                             "SELECT last_verify_error FROM public.exchange_account_credentials")
    _refuses(before_db, "other objects depend on it")
