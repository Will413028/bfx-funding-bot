"""``e4f5a6b7c8d9``: the uncertainty requests' pre-switch evidence columns have no writer.

At head ``bfx_webapi`` may not INSERT ``reconcile_event_seq`` and ``bfx_bot`` may not UPDATE
``resolved_event_seq``; the epoch trigger function no longer carries the reconcile branch (the
grant now refuses first) and keeps the observation branch. The downgrade restores the grants and
the function exactly as a database built only up to ``d3e4f5a6b7c8`` has them, and rows keep
their pre-switch values either way.

Mutation checks (one at a time; revert after each):

* Skip the REVOKE of ``bfx_bot``'s UPDATE (``resolved_event_seq``):
  ``test_head_closes_the_pre_switch_columns`` fails.
* Leave the reconcile branch in the upgraded function: the same test fails.
* Grant only one column back in the downgrade, or recreate the function with other text:
  ``test_downgrade_restores_the_previous_revision_exactly`` fails.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import _A, _build, _seed, append_epoch
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_PREVIOUS = "d3e4f5a6b7c8"
_TABLE = "public.uncertainty_resolution_requests"
_FUNCTION = "guard_uncertainty_request_evidence_epoch"


def _build_previous(url: str) -> None:
    """``test_ledger_schema_roles._build`` stopped at the previous revision."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
            "THEN CREATE ROLE bfx_webauth; END IF; END $$")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_webauth")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_webauth")
    engine.dispose()
    alembic(url, "upgrade", _PREVIOUS)
    stamp_realm(url, "ci")


@pytest.fixture
def head(pg_templates, pg_clone):
    engine = create_engine(pg_clone(pg_templates.template("ledger_s1_roles", _build)))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def previous(pg_templates, pg_clone):
    engine = create_engine(pg_clone(pg_templates.template("request_evidence_previous",
                                                          _build_previous)))
    try:
        yield engine
    finally:
        engine.dispose()


def _can(engine: Engine, role: str, column: str, privilege: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.scalar(text(
            "SELECT has_column_privilege(:r, :t, :c, :p)"),
            {"r": role, "t": _TABLE, "c": column, "p": privilege}))


def _state(engine: Engine) -> tuple[object, ...]:
    """The table's ACL, every column's ACL, and the function's text and ACL."""
    with engine.connect() as conn:
        return (
            conn.scalar(text("SELECT relacl::text FROM pg_class WHERE oid = CAST(:t AS regclass)"),
                        {"t": _TABLE}),
            conn.execute(text(
                "SELECT attname, attacl::text FROM pg_attribute "
                "WHERE attrelid = CAST(:t AS regclass) AND attnum > 0 AND NOT attisdropped "
                "ORDER BY attname"), {"t": _TABLE}).all(),
            conn.execute(text(
                "SELECT prosrc, proacl::text, proconfig::text FROM pg_proc "
                "WHERE proname = :f AND pronamespace = 'public'::regnamespace"),
                {"f": _FUNCTION}).all(),
        )


def _source(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(text(
            "SELECT prosrc FROM pg_proc WHERE proname = :f "
            "AND pronamespace = 'public'::regnamespace"), {"f": _FUNCTION}))


def _webapi_reconcile_insert(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(
            f"INSERT INTO {_TABLE}(request_id, exchange_account_id, deployment_environment, "
            "uncertainty_id, action, reconcile_event_seq, requested_by, created_at_ms) "
            f"VALUES ('{uuid4()}', '{_A}', 'ci', '{uuid4()}', 'mark_not_accepted', 7, 'op', 1)")


def test_head_closes_the_pre_switch_columns(head) -> None:
    assert not _can(head, "bfx_webapi", "reconcile_event_seq", "INSERT")
    assert not _can(head, "bfx_bot", "resolved_event_seq", "UPDATE")
    # The rest of each role's column split is untouched.
    assert _can(head, "bfx_webapi", "observation_id", "INSERT")
    assert _can(head, "bfx_bot", "outcome_reason", "UPDATE")
    source = _source(head)
    assert "reconcile" not in source
    assert "observation evidence requires ledger authority" in source
    with head.begin() as conn:
        _seed(conn)
    with pytest.raises(Exception, match="permission denied"):
        _webapi_reconcile_insert(head)


def test_downgrade_restores_the_previous_revision_exactly(head, previous) -> None:
    url = head.url.render_as_string(hide_password=False)
    with head.begin() as conn:
        _seed(conn)
        # A pre-switch request (the owner writes the shape no role may write any more).
        conn.exec_driver_sql(
            f"INSERT INTO {_TABLE}(request_id, exchange_account_id, deployment_environment, "
            "uncertainty_id, action, reconcile_event_seq, requested_by, created_at_ms) "
            f"VALUES ('{uuid4()}', '{_A}', 'ci', '{uuid4()}', 'mark_not_accepted', 7, 'op', 1)")
    head.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    assert _state(head) == _state(previous)
    assert _can(head, "bfx_webapi", "reconcile_event_seq", "INSERT")
    assert _can(head, "bfx_bot", "resolved_event_seq", "UPDATE")
    # The restored branch refuses the web API's reconcile evidence under the ledger epoch.
    with head.begin() as conn:
        append_epoch(conn, "ledger", "switch")
    with pytest.raises(Exception, match="closed under ledger authority"):
        _webapi_reconcile_insert(head)
    head.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    assert not _can(head, "bfx_webapi", "reconcile_event_seq", "INSERT")
    with head.connect() as conn:  # the pre-switch row keeps its value through both steps
        assert conn.execute(text(f"SELECT reconcile_event_seq FROM {_TABLE}")).scalars().all() == [7]
