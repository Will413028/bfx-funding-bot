"""``release_archive`` at head is exactly what its migrations left: no later revision alters it.

``c74d45a54e46`` creates the schema (the retired release ceremony's four tables and the
manifest); ``5b1e7c9d2a40`` is the last revision that writes it (probation, build approvals and
the old control requests). ``alembic check`` does not reflect the schema (``alembic/env.py``
``include_name``) and ``archive_frozen`` refuses rows, not DDL, so nothing else would notice a
later revision changing it.

Mutation check (revert after): ``ALTER TABLE release_archive.trading_state_probation ALTER
COLUMN probation_multiplier TYPE numeric(10,4)`` on the head database before the comparison:
``test_no_later_migration_alters_the_release_archive`` fails.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests.pg_templates import alembic

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_LAST_WRITER = "5b1e7c9d2a40"
# Written out, not read from the migrations: losing or gaining a table must fail here.
TABLES = {
    # c74d45a54e46
    "manifest", "trading_halt", "canary_command_permits", "release_sessions",
    "release_session_audit",
    # 5b1e7c9d2a40
    "trading_state_probation", "deployment_approvals", "trading_control_requests_v1",
}


def _build_at_last_writer(url: str) -> None:
    """The production-shaped roles of ``ledger_db``, migrated from empty to ``_LAST_WRITER``."""
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
    alembic(url, "upgrade", _LAST_WRITER)


def _catalog(engine: Engine) -> set[tuple[Any, ...]]:
    """Every column of every table in ``release_archive``: its shape, nothing of its rows.

    ``format_type`` carries length, precision, array and enum detail that
    ``information_schema.columns.data_type`` drops (``numeric`` vs ``numeric(20,8)``).
    """
    with engine.connect() as conn:
        return {tuple(row) for row in conn.execute(text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "a.attnum, pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE c.relnamespace = 'release_archive'::regnamespace AND c.relkind = 'r' "
            "AND a.attnum > 0 AND NOT a.attisdropped"))}


def test_no_later_migration_alters_the_release_archive(
    ledger_db, pg_templates, pg_clone,  # noqa: F811
) -> None:
    archived = create_engine(pg_clone(pg_templates.template(
        "release_archive_at_last_writer", _build_at_last_writer)))
    try:
        expected = _catalog(archived)
    finally:
        archived.dispose()
    assert {row[0] for row in expected} == TABLES
    assert _catalog(ledger_db) == expected
