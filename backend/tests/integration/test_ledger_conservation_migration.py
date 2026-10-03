"""a3b4c5d6e7f8: the conservation verdict columns on the accepted basis symbol rows.

Mutations (apply one at a time in the migration, run this file, revert):

* backfill ``baseline`` as ``conserved`` / drop the server default step:
  ``test_upgrade_backfills_baseline_and_leaves_no_default``.
* downgrade without the verdict guard: ``test_downgrade_refuses_to_drop_stored_verdicts``.
* drop the CHECK: ``test_check_constraint_rejects_inconsistent_verdicts``.
* leave the table writable (skip nothing: the existing immutability triggers cover the new
  columns): ``test_verdict_is_immutable_and_only_the_bot_writes_it``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.pg_templates import alembic

from .test_ledger_schema_roles import _B, _seed, ledger_db, seeded  # noqa: F401 - fixtures

pytestmark = pytest.mark.integration

_PARENT = "e1f2a3b4c5d7"
_NEW = ("conservation", "lent_unexplained", "foreign_executed")
_TABLE = "accepted_capital_basis_symbol"


def _columns(conn) -> set[str]:
    return {column["name"] for column in inspect(conn).get_columns(_TABLE)}


def _insert(conn, conservation: str, unexplained: str, foreign: str, symbol: str = "fX") -> None:
    conn.execute(
        text(
            f"INSERT INTO {_TABLE}(basis_id, symbol, available, offered, credits, "
            "unattributed_credits, foreign_offers, conservation, lent_unexplained, "
            "foreign_executed) VALUES (:b, :s, 0, 0, 0, 0, 0, :c, :u, :f)"
        ),
        {"b": _B, "s": symbol, "c": conservation, "u": unexplained, "f": foreign},
    )


def test_upgrade_backfills_baseline_and_leaves_no_default(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "downgrade", _PARENT)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            assert not set(_NEW) & _columns(conn)
            _seed(conn, pre_verdict=True)
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT conservation, lent_unexplained, foreign_executed FROM {_TABLE}")
            ).one()
            assert (row[0], row[1], row[2]) == ("baseline", 0, 0)
            defaults = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = ANY(:c)"
                ),
                {"t": _TABLE, "c": list(_NEW)},
            ).scalars().all()
            assert defaults == [None, None, None]
            assert all(
                column["nullable"] is False
                for column in inspect(conn).get_columns(_TABLE)
                if column["name"] in _NEW
            )
    finally:
        engine.dispose()


def test_downgrade_drops_baseline_only_rows_and_round_trips(seeded) -> None:  # noqa: F811
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    alembic(url, "downgrade", _PARENT)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert not set(_NEW) & _columns(conn)
            assert conn.scalar(text(f"SELECT count(*) FROM {_TABLE}")) == 1
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        with engine.connect() as conn:
            assert set(_NEW) <= _columns(conn)
    finally:
        engine.dispose()


def test_downgrade_refuses_to_drop_stored_verdicts(seeded) -> None:  # noqa: F811
    with seeded.begin() as conn:
        _insert(conn, "conserved", "0", "0")
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    with pytest.raises(Exception, match="refuse downgrade of populated ledger"):
        alembic(url, "downgrade", _PARENT)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert set(_NEW) <= _columns(conn)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("conservation", "unexplained", "foreign", "accepted"),
    [
        ("baseline", "0", "0", True),
        ("conserved", "0", "0", True),
        ("conserved", "0.01", "5", True),
        ("foreign_lending", "100", "100", True),
        ("unexplained_lending", "100", "0", True),
        ("baseline", "1", "0", False),
        ("baseline", "0", "1", False),
        ("conserved", "-1", "0", False),
        ("conserved", "0", "-1", False),
        ("foreign_lending", "100", "0", False),
        ("foreign_lending", "0", "100", False),
        ("unexplained_lending", "0", "0", False),
        ("sideways", "0", "0", False),
    ],
)
def test_check_constraint_rejects_inconsistent_verdicts(
    seeded,  # noqa: F811
    conservation,
    unexplained,
    foreign,
    accepted,
) -> None:
    if accepted:
        with seeded.begin() as conn:
            _insert(conn, conservation, unexplained, foreign)
        return
    with (
        pytest.raises(Exception, match="ck_accepted_basis_symbol_conservation"),
        seeded.begin() as conn,
    ):
        _insert(conn, conservation, unexplained, foreign)


def test_verdict_is_immutable_and_only_the_bot_writes_it(seeded) -> None:  # noqa: F811
    for statement in (
        f"UPDATE {_TABLE} SET conservation = 'conserved'",
        f"UPDATE {_TABLE} SET lent_unexplained = 1",
        f"DELETE FROM {_TABLE}",
    ):
        with pytest.raises(Exception, match=r"immutable|permission denied"), seeded.begin() as conn:
            conn.exec_driver_sql(statement)
    with seeded.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "VALUES (2, 'ledger', 2, 'test', 'conservation grants')"
        )
    with seeded.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _insert(conn, "conserved", "0", "0", symbol="fBot")
        assert conn.scalar(text(f"SELECT conservation FROM {_TABLE} WHERE symbol = 'fBot'")) == (
            "conserved"
        )
    for role in ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
        for column in _NEW:
            assert not seeded.connect().scalar(
                text("SELECT has_column_privilege(:r, :t, :c, 'SELECT')"),
                {"r": role, "t": _TABLE, "c": column},
            ), (role, column)
        with pytest.raises(Exception, match="permission denied"), seeded.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            _insert(conn, "conserved", "0", "0", symbol="fNo")
