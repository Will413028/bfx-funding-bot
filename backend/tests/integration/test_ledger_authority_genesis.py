"""The ledger genesis migration (``b1c2d3e4f5a6``) on clone PostgreSQL.

A database whose latest epoch is ``legacy`` is decided once, by its legacy history: none
appends a ``ledger`` epoch; any refuses the upgrade (seeded or not); a database already on
the ledger is left as it is.

Mutation checks (one at a time; revert after each):

* Drop the ``event_log`` check from ``upgrade``: ``test_legacy_history_without_a_seed_refuses``
  and ``test_seeded_legacy_history_that_was_never_switched_refuses`` fail (the ledger epoch
  is appended over unseeded capital).
* Drop the early return on a ``ledger`` latest epoch: ``test_a_switched_database_is_left_as_it_is``
  and the re-upgrade in ``test_a_database_without_legacy_history_starts_on_the_ledger`` fail
  (a second ``ledger`` row is appended).
* Drop the ``event_log`` check from ``downgrade``:
  ``test_downgrade_keeps_the_genesis_once_history_exists`` fails (the epoch a database with
  history ran under is deleted).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from bfx_funding_bot.core.schema_head import migration_scripts
from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import _A, _seed
from .test_ledger_seed_schema import _owner_seed_observation

pytestmark = pytest.mark.integration

_REVISION = "b1c2d3e4f5a6"
# Whatever the genesis is stacked on: the database the upgrade starts from.
_PREVIOUS = str(migration_scripts().get_revision(_REVISION).down_revision)


def _build(url: str) -> None:
    alembic(url, "upgrade", _PREVIOUS)
    stamp_realm(url, "ci")


@pytest.fixture
def previous_db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template(f"genesis_before_{_PREVIOUS}", _build))
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()


def _epochs(engine: Engine) -> list[tuple[int, str, str]]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(
            "SELECT epoch_seq, authority, actor FROM capital_authority_epoch ORDER BY epoch_seq"))]


def _head(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(text("SELECT version_num FROM alembic_version")))


def _legacy_history(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts(id,venue,label) VALUES ('{_A}','bitfinex','legacy') "
            "ON CONFLICT DO NOTHING")
        conn.exec_driver_sql(
            "INSERT INTO event_log(account_id, exchange_account_id, deployment_environment, "
            f"event_type, payload, occurred_at_ms) VALUES ('{_A}', '{_A}', 'ci', 'x', '{{}}', 1)")


def test_a_database_without_legacy_history_starts_on_the_ledger(previous_db) -> None:
    url, engine = previous_db
    assert _epochs(engine) == [(1, "legacy", "migration f6a7b8c9d0e1")]
    alembic(url, "upgrade", _REVISION)
    assert _epochs(engine) == [
        (1, "legacy", "migration f6a7b8c9d0e1"),
        (2, "ledger", f"migration {_REVISION} genesis"),
    ]
    # Reversible while nothing ran on it: downgrade takes back exactly the genesis row.
    alembic(url, "downgrade", _PREVIOUS)
    assert _epochs(engine) == [(1, "legacy", "migration f6a7b8c9d0e1")]
    alembic(url, "upgrade", _REVISION)
    assert [row[1] for row in _epochs(engine)] == ["legacy", "ledger"]


def test_downgrade_keeps_the_genesis_under_a_later_epoch(previous_db) -> None:
    url, engine = previous_db
    alembic(url, "upgrade", _REVISION)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "VALUES (3, 'ledger', 3, 'switch', 'later')")
    alembic(url, "downgrade", _PREVIOUS)
    assert [row[2] for row in _epochs(engine)][1:] == [f"migration {_REVISION} genesis", "switch"]


def test_downgrade_keeps_the_genesis_once_history_exists(previous_db) -> None:
    url, engine = previous_db
    alembic(url, "upgrade", _REVISION)
    _legacy_history(engine)
    alembic(url, "downgrade", _PREVIOUS)
    assert [row[1] for row in _epochs(engine)] == ["legacy", "ledger"]


def test_legacy_history_without_a_seed_refuses(previous_db) -> None:
    url, engine = previous_db
    _legacy_history(engine)
    with pytest.raises(RuntimeError, match="without a legacy_seed observation"):
        alembic(url, "upgrade", _REVISION)
    assert _head(engine) == _PREVIOUS
    assert [row[1] for row in _epochs(engine)] == ["legacy"]


def test_seeded_legacy_history_that_was_never_switched_refuses(previous_db) -> None:
    url, engine = previous_db
    with engine.begin() as conn:
        _seed(conn)
        _owner_seed_observation(conn)
    _legacy_history(engine)
    with pytest.raises(RuntimeError, match="seeded but never switched"):
        alembic(url, "upgrade", _REVISION)
    assert [row[1] for row in _epochs(engine)] == ["legacy"]


def test_a_switched_database_is_left_as_it_is(previous_db) -> None:
    url, engine = previous_db
    _legacy_history(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "VALUES (2, 'ledger', 2, 'switch', 'switch')")
    alembic(url, "upgrade", _REVISION)
    assert _head(engine) == _REVISION
    assert _epochs(engine) == [(1, "legacy", "migration f6a7b8c9d0e1"), (2, "ledger", "switch")]
