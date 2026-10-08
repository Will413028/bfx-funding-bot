"""2e835b6f4c12 on real PostgreSQL: the transition guard refuses REPEATABLE READ, one scope index.

Mutation checks (one at a time; revert after each):

* Drop the isolation check from the guard: ``test_a_repeatable_read_writer_is_refused``
  inserts, and the upgrade's own postcondition raises.
* Keep ``uq_trading_state_scope`` (skip its DROP): the upgrade's index postcondition raises.
* Drop the precondition on the scope indexes: ``test_the_upgrade_refuses_unexpected_scope_indexes``
  fails on a different error (the DROP of a missing index).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.pg_templates import alembic, template_at

from .test_ledger_schema_roles import _prepare
from .test_operator_request_causation import _audit, _seeded, _state

pytestmark = pytest.mark.integration

_BEFORE = "41cec7caf291"
_REVISION = "2e835b6f4c12"
_GUARD = ("SELECT prosrc FROM pg_proc "
          "WHERE oid = 'public.guard_trading_state_transition()'::regprocedure")
_ADDED = """      -- The latest row is read after the lock below; a REPEATABLE READ snapshot predates
      -- the lock and would judge the transition against a stale state.
      IF current_setting('transaction_isolation') = 'repeatable read' THEN
        RAISE EXCEPTION 'trading_state is written under READ COMMITTED or SERIALIZABLE'
          USING ERRCODE = 'BX004';
      END IF;
"""


@pytest.fixture
def before(pg_templates, pg_clone):
    """41cec7caf291, seeded with a trading state and its cancel-all audit; the upgrade runs in
    the test."""
    yield from _seeded(pg_clone(pg_templates.template(
        "ledger_s1_roles_product_drop", template_at(_BEFORE, _prepare))))


@pytest.fixture
def upgraded(before):
    url, engine = before
    alembic(url, "upgrade", _REVISION)
    return url, engine


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def _scope_indexes(conn) -> list[tuple[str, bool]]:
    return [tuple(row) for row in conn.execute(text(
        "SELECT indexrelid::regclass::text, indisunique FROM pg_index "
        "WHERE indrelid = 'public.trading_state'::regclass AND indnkeyatts = 3 ORDER BY 1"))]


def test_the_upgrade_keeps_one_unique_scope_index_under_the_audit_key(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        # Rows the rebuilt foreign key validates.
        _audit(conn, state_id=_state(conn, at=100), actor="operator", currency="UST", at=100)
        guard = conn.scalar(text(_GUARD))

    alembic(url, "upgrade", _REVISION)

    with engine.connect() as conn:
        assert _scope_indexes(conn) == [("ix_trading_state_scope_id", True)]
        assert conn.execute(text(
            "SELECT convalidated, conindid::regclass::text FROM pg_constraint "
            "WHERE conname = 'fk_funding_cancel_all_audit_trading_state'")).one() == (
            True, "ix_trading_state_scope_id")
        # The guard is 8e4b2f6a1c37's with the isolation check first, and nothing else changed.
        upgraded = conn.scalar(text(_GUARD))
    head, _, tail = upgraded.partition(_ADDED)
    assert tail and head + tail == guard


def test_the_upgrade_refuses_unexpected_scope_indexes(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_trading_state_scope_id")
    with pytest.raises(Exception, match="scope indexes are not as 5e820d6dc7da left them"):
        alembic(url, "upgrade", _REVISION)
    assert _version(engine) == _BEFORE


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "SERIALIZABLE"])
def test_a_read_committed_or_serializable_writer_is_admitted(upgraded, isolation) -> None:
    _, engine = upgraded
    with engine.begin() as conn:
        conn.exec_driver_sql(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
        assert _state(conn, state="HALTED", reason=isolation)


def test_a_repeatable_read_writer_is_refused(upgraded) -> None:
    _, engine = upgraded
    with engine.connect() as conn:
        before = conn.scalar(text("SELECT count(*) FROM trading_state"))
    with pytest.raises(DBAPIError) as refused, engine.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        _state(conn, state="HALTED", reason="stale snapshot")
    assert refused.value.orig.sqlstate == "BX004"
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == before
