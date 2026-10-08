"""41cec7caf291 on real PostgreSQL roles: the request tables' product columns go, nothing is lost.

The upgrade starts from 5e820d6dc7da, where each effect already names its request; it refuses
while a request's product column says something its product does not say back.

Mutation checks (one at a time; revert after each):

* Drop either entry of ``PRECONDITIONS``: its violation case upgrades instead of raising.
* Drop the outcome CHECK check: ``test_a_check_naming_a_dropped_column_refuses_the_upgrade``
  upgrades with the CHECK gone.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.pg_templates import alembic, template_at

from .test_ledger_schema_roles import _P, _prepare
from .test_operator_request_causation import (
    _capital,
    _refused,
    _revision,
    _seeded,
    _state,
    _trading,
)

pytestmark = pytest.mark.integration

_BEFORE = "5e820d6dc7da"
_REVISION = "41cec7caf291"

# What the previous web API image (0b114f91) selects from each request table: every column its
# models mapped (`git show 0b114f91:backend/src/bfx_funding_bot/modules/execution/...`).
_PREVIOUS_WEBAPI_COLUMNS = {
    "trading_control_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms", "state", "processed_at_ms", "outcome_reason"),
    "capital_policy_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms", "state", "processed_at_ms", "outcome_reason"),
}


@pytest.fixture
def before(pg_templates, pg_clone):
    """5e820d6dc7da, seeded; the upgrade runs in the test."""
    yield from _seeded(pg_clone(pg_templates.template(
        "ledger_s1_roles_causation", template_at(_BEFORE, _prepare))))


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def _applied(conn, request: str, column: str, value: object, table: str, *, at: int = 99) -> None:
    """The owner settles ``request`` with its product (5e820d6dc7da left the column, closed to
    the bot)."""
    conn.exec_driver_sql(f"UPDATE {table} SET state='applied', processed_at_ms={at}, "
                         f"{column}={value} WHERE request_id='{request}'")


def test_the_upgrade_drops_the_columns_and_keeps_the_outcome_checks(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        # A kill that wrote its HALTED (linked both ways), and one that only restated it.
        writer = _trading(conn, action="kill", reason="a", created=90)
        state_id = _state(conn, reason="kill: a", at=100, request=writer)
        _applied(conn, writer, "trading_state_id", state_id, "trading_control_requests")
        restater = _trading(conn, action="kill", reason="a", created=120)
        _applied(conn, restater, "trading_state_id", state_id, "trading_control_requests", at=125)
        # An enable that wrote its revision, and an unchanged one naming the revision in force.
        enable = _capital(conn, action="enable")
        revision = _revision(conn, revision=2, source=f'{{"request_id": "{enable}"}}', request=enable)
        _applied(conn, enable, "policy_revision_id", f"'{revision}'", "capital_policy_requests")
        unchanged = _capital(conn, action="enable")
        _applied(conn, unchanged, "policy_revision_id", f"'{revision}'", "capital_policy_requests")
        # A request applied since 5e820d6dc7da names nothing.
        _trading(conn, settle="state='rejected', processed_at_ms=2, outcome_reason='x'")
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND (table_name, column_name) IN (('trading_control_requests', 'trading_state_id'), "
            "('capital_policy_requests', 'policy_revision_id'))")) == 0
        checks = dict(conn.execute(text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid IN ('public.trading_control_requests'::regclass, "
            "'public.capital_policy_requests'::regclass) AND contype = 'c' "
            "AND conname LIKE '%\\_outcome'")).all())
        assert len(checks) == 2
    with engine.begin() as conn:
        waiting = _trading(conn)
    _refused(engine, "UPDATE trading_control_requests SET state = 'rejected', processed_at_ms = 2 "
             f"WHERE request_id = '{waiting}'", "ck_trading_control_requests_outcome", role="bfx_bot")


def test_the_previous_web_api_still_reads_what_it_maps(before) -> None:
    url, engine = before
    alembic(url, "upgrade", "head")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        for table, columns in _PREVIOUS_WEBAPI_COLUMNS.items():
            conn.exec_driver_sql(f"SELECT {', '.join(columns)} FROM {table}")


def _trading_writer_unlinked(conn) -> None:
    writer = _trading(conn, action="kill", reason="a", created=90)
    state_id = _state(conn, reason="kill: a", at=100)  # names no request
    _applied(conn, writer, "trading_state_id", state_id, "trading_control_requests")


def _capital_writer_unlinked(conn) -> None:
    enable = _capital(conn, action="enable")
    revision = _revision(conn, revision=2, source=f'{{"request_id": "{enable}"}}')  # untyped
    _applied(conn, enable, "policy_revision_id", f"'{revision}'", "capital_policy_requests")


_VIOLATIONS = {
    "trading request that wrote its trading state, which does not name it": _trading_writer_unlinked,
    "capital request that wrote its revision, which does not name it": _capital_writer_unlinked,
}


def test_every_precondition_has_a_violation_case() -> None:
    from .test_database_realm import _load

    names = [what for what, _sql in _load(f"{_REVISION}_drop_request_product_columns.py").PRECONDITIONS]
    assert sorted(names) == sorted(_VIOLATIONS)


@pytest.mark.parametrize("precondition", sorted(_VIOLATIONS))
def test_each_precondition_refuses_the_upgrade_and_names_the_count(before, precondition) -> None:
    url, engine = before
    with engine.begin() as conn:
        _VIOLATIONS[precondition](conn)
    with pytest.raises(RuntimeError, match=f"{precondition}: 1"):
        alembic(url, "upgrade", "head")
    assert _version(engine) == _BEFORE


def test_a_restated_or_unchanged_request_loses_nothing(before) -> None:
    """Its product column named a row someone else wrote: dropping it is allowed (D9)."""
    url, engine = before
    with engine.begin() as conn:
        admin = _state(conn, actor="admin-api", reason="manual", at=100)
        kill = _trading(conn, action="kill", reason="a", created=120)
        _applied(conn, kill, "trading_state_id", admin, "trading_control_requests")
        unchanged = _capital(conn, action="disable")
        _applied(conn, unchanged, "policy_revision_id", f"'{_P}'", "capital_policy_requests")
    alembic(url, "upgrade", "head")
    assert _version(engine) == _REVISION


def test_a_check_naming_a_dropped_column_refuses_the_upgrade(before) -> None:
    """PostgreSQL drops a CHECK with a column it names; the migration notices."""
    url, engine = before
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE trading_control_requests DROP CONSTRAINT ck_trading_control_requests_outcome, "
            "ADD CONSTRAINT ck_trading_control_requests_outcome CHECK "
            "(state <> 'requested' OR trading_state_id IS NULL)")
    with pytest.raises(RuntimeError, match="an outcome CHECK went with the dropped columns: 1 of 2"):
        alembic(url, "upgrade", "head")
    assert _version(engine) == _BEFORE
