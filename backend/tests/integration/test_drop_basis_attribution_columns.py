"""dc9d9290c2c0: a basis loses ``unattributed_credits`` and ``attribution_basis``.

On a disposable PostgreSQL built like production's host (web API with its exact allowlist),
from the previous revision with a basis whose rows carry both columns: the upgrade keeps every
row and its other values, removes both columns and the attribution CHECK, rebuilds the amount
CHECK under its name, and leaves the web API reading the same columns less the dropped one.

Mutations (each in a copy of the migration; the named test fails):

* rebuild ``ck_accepted_basis_symbol_amount`` without one of its four terms:
  ``test_the_amount_check_still_refuses_a_negative_amount`` for that column;
* do not rebuild it at all: the same test, at setup (the migration's own end assertion);
* drop the function-body assertion:
  ``test_a_function_that_reads_a_dropped_column_refuses_the_upgrade``;
* skip one ``DROP COLUMN``: ``test_the_upgrade_keeps_the_rows_and_drops_both_columns`` (and the
  migration's own end assertion).
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import _B, _seed
from .test_webapi_privilege_allowlist import _build_prod_faithful

pytestmark = pytest.mark.integration

_REVISION = "dc9d9290c2c0"
_BEFORE = "545b2877e9bd"
_SYMBOL = "accepted_capital_basis_symbol"
_CREDIT = "accepted_capital_basis_credit"
_DROPPED = {_SYMBOL: "unattributed_credits", _CREDIT: "attribution_basis"}


def _build_before(url: str) -> None:
    _build_prod_faithful(url, target=_BEFORE)
    stamp_realm(url, "ci")


@pytest.fixture
def before(pg_templates, pg_clone) -> Iterator[tuple[str, Engine]]:
    """The previous revision with one seeded basis: a symbol row and a credit row."""
    url = pg_clone(pg_templates.template("drop_basis_attribution_before", _build_before))
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)
    try:
        yield url, engine
    finally:
        engine.dispose()


@pytest.fixture
def upgraded(before: tuple[str, Engine]) -> Engine:
    url, engine = before
    alembic(url, "upgrade", _REVISION)
    return engine


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = :t"), {"t": table}).scalars())


def _webapi_reads(conn, table: str) -> set[str]:
    return {column for column in _columns(conn, table) if conn.scalar(text(
        "SELECT has_column_privilege('bfx_webapi', :t, :c, 'SELECT')"),
        {"t": f"public.{table}", "c": column})}


def _rows(conn, table: str) -> list[dict[str, object]]:
    """Every row of ``table`` without the dropped column, in a stable order."""
    rows = [dict(row) for row in conn.execute(text(f"SELECT * FROM {table}")).mappings()]
    for row in rows:
        row.pop(_DROPPED[table], None)
    return sorted(rows, key=repr)


def test_the_upgrade_keeps_the_rows_and_drops_both_columns(before: tuple[str, Engine]) -> None:
    url, engine = before
    with engine.connect() as conn:
        kept = {table: _rows(conn, table) for table in _DROPPED}
        for table, column in _DROPPED.items():
            assert column in _columns(conn, table)
            assert conn.scalar(text(f"SELECT count({column}) FROM {table}")) == 1

    alembic(url, "upgrade", _REVISION)

    with engine.connect() as conn:
        for table, column in _DROPPED.items():
            assert column not in _columns(conn, table)
            assert _rows(conn, table) == kept[table]
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'ck_accepted_basis_credit_attribution'")) == 0
        assert conn.scalar(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_accepted_basis_symbol_amount'")) == (
            "CHECK (((available >= (0)::numeric) AND (offered >= (0)::numeric) AND "
            "(credits >= (0)::numeric) AND (foreign_offers >= (0)::numeric)))")
    # ``check`` compares against head, whatever revision comes after this one.
    alembic(url, "upgrade", "head")
    alembic(url, "check")


def test_a_function_that_reads_a_dropped_column_refuses_the_upgrade(
        before: tuple[str, Engine]) -> None:
    url, engine = before
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE FUNCTION public.reads_attribution() RETURNS numeric LANGUAGE plpgsql AS "
            f"$$ BEGIN RETURN (SELECT sum(unattributed_credits) FROM {_SYMBOL}); END $$")

    with pytest.raises(RuntimeError, match=r"functions still read a dropped column: "
                                           r"public\.reads_attribution"):
        alembic(url, "upgrade", _REVISION)

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _BEFORE
        assert _DROPPED[_SYMBOL] in _columns(conn, _SYMBOL)


@pytest.mark.parametrize("column", ["available", "offered", "credits", "foreign_offers"])
def test_the_amount_check_still_refuses_a_negative_amount(upgraded: Engine, column: str) -> None:
    values = {"available": 1, "offered": 1, "credits": 1, "foreign_offers": 1, column: -1}
    with pytest.raises(DBAPIError, match="ck_accepted_basis_symbol_amount"), \
            upgraded.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO {_SYMBOL} (basis_id, symbol, available, offered, credits, "
            "foreign_offers, conservation, lent_unexplained, foreign_executed, fill_conflicts) "
            f"VALUES ('{_B}', 'fUSD', {values['available']}, {values['offered']}, "
            f"{values['credits']}, {values['foreign_offers']}, 'baseline', 0, 0, 0)")


def test_the_web_api_reads_the_same_columns_less_the_dropped_one(
        before: tuple[str, Engine]) -> None:
    url, engine = before
    with engine.connect() as conn:
        assert _webapi_reads(conn, _SYMBOL) == {
            "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits"}

    alembic(url, "upgrade", _REVISION)

    with engine.connect() as conn:
        assert _webapi_reads(conn, _SYMBOL) == {
            "basis_id", "symbol", "available", "offered", "credits"}
        assert _webapi_reads(conn, _CREDIT) == {"basis_id", "symbol"}
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        assert conn.exec_driver_sql(
            f"SELECT basis_id, symbol, available, offered, credits FROM {_SYMBOL}").all()


def test_the_downgrade_is_refused(upgraded: Engine) -> None:
    url = upgraded.url.render_as_string(hide_password=False)
    with pytest.raises(RuntimeError, match="dc9d9290c2c0 is irreversible"):
        alembic(url, "downgrade", _BEFORE)
