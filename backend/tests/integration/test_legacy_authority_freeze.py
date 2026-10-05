"""The legacy freeze trigger (``e8f9a0b1c2d3``) on clone PostgreSQL with prod default grants.

Each write is a zero-row statement as ``bfx_bot`` (``INSERT ... SELECT ... WHERE false``,
``UPDATE ... WHERE false``, ``DELETE ... WHERE false``): a statement trigger fires on it, so
the statement shows the trigger's decision without any table's row constraints. A write the
role holds no privilege for is refused by the privilege check first; the catalog test pins
that the trigger covers it anyway.

Mutation checks (one at a time; revert after each):

* Drop a table from the migration's ``FROZEN``: its ``test_frozen_*`` case passes the write
  under ``ledger`` and ``test_the_trigger_is_on_exactly_the_frozen_tables`` fails.
* Compare the epoch with ``IS DISTINCT FROM 'ledger'`` (the ledger guard's sense): every
  write under ``legacy`` is refused.
* Drop the owner exemption: the owner's write under ``ledger`` is refused.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from tests.pg_templates import alembic

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture

pytestmark = pytest.mark.integration

_REVISION = "e8f9a0b1c2d3"
_PREVIOUS = "d7e8f9a0b1c2"
# Written out, not imported from the migration: shrinking the migration's list must fail here.
FROZEN = (
    "event_log", "event_prefix_hashes", "projection_heads", "position_state",
    "venue_offer_state", "venue_credit_state", "reconcile_observation", "offer_claims",
    "submission_attempts", "execution_uncertainties", "capital_snapshot_queries",
    "capital_snapshots",
)
# Shared tables in public that must stay writable after the switch.
SHARED = (
    "execution_decisions", "trading_state", "funding_cancel_all_audit", "nav_peak",
    "nav_window_samples", "diagnostics", "uncertainty_resolution_requests",
    "capital_policy_requests", "trading_control_requests",
)
# Tables outside the execution module the switched runtime keeps writing (spot checks).
OUTSIDE = ("funding_trades", "funding_credit_history", "funding_interest_payments",
           "funding_candles", "attribution_weekly")
_OPERATIONS = ("INSERT", "UPDATE", "DELETE")


def _column(conn: Connection, table: str, operation: str) -> str:
    """A plain column bfx_bot may write by ``operation`` (outboxes grant column UPDATE only)."""
    privilege = "SELECT" if operation == "DELETE" else operation
    return str(conn.scalar(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name=:t AND is_generated='NEVER' AND identity_generation IS DISTINCT FROM "
        "'ALWAYS' AND has_column_privilege('bfx_bot', 'public.' || :t, column_name, :p) "
        "ORDER BY ordinal_position LIMIT 1"), {"t": table, "p": privilege}))


def _statement(conn: Connection, table: str, operation: str) -> str:
    column = _column(conn, table, operation)
    return {
        "INSERT": f"INSERT INTO public.{table} ({column}) SELECT {column} FROM public.{table} "
                  "WHERE false",
        "UPDATE": f"UPDATE public.{table} SET {column} = {column} WHERE false",
        "DELETE": f"DELETE FROM public.{table} WHERE false",
    }[operation]


def _held(engine: Engine, table: str) -> list[str]:
    """The operations bfx_bot holds on ``table`` (prod default privileges, as migrated)."""
    with engine.connect() as conn:
        return [op for op in _OPERATIONS if conn.scalar(
            text("SELECT has_table_privilege('bfx_bot', :t, :p)" if op == "DELETE" else
                 "SELECT has_any_column_privilege('bfx_bot', :t, :p)"),
            {"t": f"public.{table}", "p": op})]


def _write(engine: Engine, table: str, operation: str, *, role: str | None = "bfx_bot") -> None:
    with engine.begin() as conn:
        statement = _statement(conn, table, operation)
        if role is not None:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        conn.exec_driver_sql(statement)


def _epoch(engine: Engine, seq: int, authority: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            f"VALUES ({seq}, '{authority}', {seq}, 'test', 'switch')")


def _cases(tables: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(table, op) for table in tables for op in _OPERATIONS]


def test_the_trigger_is_on_exactly_the_frozen_tables(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.relname, (t.tgtype & 1) = 0 AS statement, (t.tgtype & 2) <> 0 AS before, "
            "(t.tgtype & 4) <> 0 AS ins, (t.tgtype & 8) <> 0 AS del, (t.tgtype & 16) <> 0 AS upd "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = 'legacy_authority_write'")).all()
        definer = conn.scalar(text(
            "SELECT prosecdef FROM pg_proc WHERE proname = 'guard_legacy_authority'"))
    assert {row.relname: tuple(row[1:]) for row in rows} == dict.fromkeys(
        FROZEN, (True, True, True, True, True))
    assert definer is False


@pytest.mark.parametrize(("table", "operation"), _cases(FROZEN))
def test_frozen_tables_refuse_the_bot_under_ledger_and_pass_it_under_legacy(
    ledger_db, table: str, operation: str,  # noqa: F811
) -> None:
    if operation not in _held(ledger_db, table):
        pytest.skip(f"bfx_bot holds no {operation} on {table}: the privilege check refuses first")
    _write(ledger_db, table, operation)  # legacy: the trigger passes
    _epoch(ledger_db, 2, "ledger")
    with pytest.raises(Exception, match=f"legacy write after the authority switch: {table}"):
        _write(ledger_db, table, operation)
    _write(ledger_db, table, operation, role=None)  # the owner still writes
    _epoch(ledger_db, 3, "legacy")  # latest wins
    _write(ledger_db, table, operation)


def test_every_frozen_table_is_writable_by_the_bot_under_legacy(ledger_db) -> None:  # noqa: F811
    """Each frozen table has at least INSERT for bfx_bot, so its refusal case above ran."""
    for table in FROZEN:
        assert "INSERT" in _held(ledger_db, table), table


@pytest.mark.parametrize("table", SHARED + OUTSIDE)
def test_shared_tables_stay_writable_after_the_switch(ledger_db, table: str) -> None:  # noqa: F811
    held = _held(ledger_db, table)
    assert held, f"bfx_bot writes {table} at runtime"
    _epoch(ledger_db, 2, "ledger")
    for operation in held:
        _write(ledger_db, table, operation)
    with ledger_db.connect() as conn:
        assert not conn.scalar(text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = 'legacy_authority_write' AND c.relname = :t"), {"t": table})


def test_downgrade_drops_the_trigger_and_upgrade_restores_it(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    _epoch(ledger_db, 2, "ledger")
    ledger_db.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    _write(ledger_db, "event_log", "INSERT")  # no freeze before this revision
    with ledger_db.connect() as conn:
        assert not conn.scalar(text(
            "SELECT count(*) FROM pg_proc WHERE proname = 'guard_legacy_authority'"))
    ledger_db.dispose()
    alembic(url, "upgrade", _REVISION)
    alembic(url, "check")
    with pytest.raises(Exception, match="legacy write after the authority switch: event_log"):
        _write(ledger_db, "event_log", "INSERT")
