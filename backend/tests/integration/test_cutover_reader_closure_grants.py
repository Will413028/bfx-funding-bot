"""Migration d7e8f9a0b1c2: the reader's new grants are exactly the allowlist, columns only.

The prod precheck shape: the reader's column privileges after minus before the revision equal
``GRANTS`` exactly (nothing else gained or lost), the group still holds no table-level grant
and no write privilege, and the downgrade restores the previous set.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine

from bfx_funding_bot.apps.capital_comparison_guard import READER_ROLE
from bfx_funding_bot.core.db import Base
from tests.pg_templates import alembic

pytestmark = pytest.mark.integration

_PREVIOUS = "c6d7e8f9a0b1"
_MIGRATION = Path(__file__).parents[2] / "alembic/versions/d7e8f9a0b1c2_cutover_reader_closure_grants.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("d7e8f9a0b1c2", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _privileges(url: str) -> tuple[set[tuple[str, str]], list[str], list[tuple[str, str]]]:
    """(SELECT columns, table-level grants, INSERT/UPDATE columns) of the reader group."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            columns = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                f"AND has_column_privilege('{READER_ROLE}', c.oid, a.attnum, 'SELECT')"
            ).all()
            tables = conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.role_table_grants "
                f"WHERE grantee = '{READER_ROLE}' AND table_schema = 'public'"
            ).scalars().all()
            writes = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                f"AND has_column_privilege('{READER_ROLE}', c.oid, a.attnum, 'INSERT,UPDATE')"
            ).all()
    finally:
        engine.dispose()
    return {(t, c) for t, c in columns}, list(tables), [(t, c) for t, c in writes]


def test_new_reader_grants_are_exactly_the_allowlist(pg_head_url: str) -> None:
    grants = _migration().GRANTS
    allowlist = {(table, column) for table, columns in grants.items() for column in columns}
    head, tables, writes = _privileges(pg_head_url)
    assert tables == [] and writes == []
    alembic(pg_head_url, "downgrade", _PREVIOUS)
    before, tables, writes = _privileges(pg_head_url)
    assert tables == [] and writes == []
    assert head - before == allowlist
    assert before <= head  # nothing revoked that an earlier revision granted
    assert not allowlist & before  # every grant is new, so the downgrade's revoke is exact
    alembic(pg_head_url, "upgrade", "head")
    assert _privileges(pg_head_url)[0] == head
    alembic(pg_head_url, "check")


def test_the_legacy_arm_reads_whole_rows_of_claims_and_trades(pg_head_url: str) -> None:
    """``_classify`` loads whole ``OfferClaimRow`` / ``FundingTradeRow`` ORM rows: the reader
    holds every mapped column of both after this revision."""
    head, _, _ = _privileges(pg_head_url)
    for table in ("offer_claims", "funding_trades"):
        mapped = {column.name for column in Base.metadata.tables[table].columns}
        assert {(table, column) for column in mapped} <= head, table
    for table, column in (
        ("submission_attempt_journal", "authorization_evidence"),
        ("ledger_observation", "evidence"),
        ("ledger_observation_offer", "raw"),
        ("quarantine_opening", "evidence"),
        ("execution_resolution_journal", "evidence"),
        ("transport_outcome_journal", "evidence"),
        ("trading_state", "reason"),
        ("uncertainty_resolution_requests", "reason"),
    ):
        assert (table, column) not in head, (table, column)
