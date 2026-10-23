"""``bfx_research``'s privileges in ``public`` and ``legacy_archive`` are an exact allowlist.

``EXPECTED`` is head's allowlist for the weekly report's and the research jobs' role
(``3c5e7a9b1d2f``): table-level SELECT on what the steps read, INSERT/UPDATE (upsert) or
INSERT/DELETE (replace) on the research tables they write, nothing else. It is checked against
the EFFECTIVE privileges of three builds (worst-case default privileges, production's host
setup, stray grants at the previous revision): a migration that grants or revokes anything for
the role must update ``EXPECTED`` in the same change. ``test_research_role_behaviour.py`` runs
the steps' real statements logged in as the role.

Mutation checks (one at a time; revert after each):

* Add ``"trading_state": ("SELECT", "INSERT")`` to ``RESEARCH_TABLE_PRIVILEGES``: the
  effective-privilege and no-bot-write tests fail.
* Remove the REVOKE steps from its ``upgrade``: the effective-privilege test fails on the
  worst-case and stray builds.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import alembic

from .test_trading_state_migration import _reset
from .test_webapi_privilege_allowlist import _effective

pytestmark = pytest.mark.integration

ROLE = "bfx_research"
_REVISION = "3c5e7a9b1d2f"
_PREVIOUS = "f836fdee9bf3"
_MIGRATION = Path(__file__).parents[2] / "alembic" / "versions" / f"{_REVISION}_research_role_allowlist.py"
_R = {"SELECT"}

# The research tables: the only ones the role may write.
WRITTEN = {
    "funding_stats": {"SELECT", "INSERT", "UPDATE"},
    "perp_funding_rates": {"SELECT", "INSERT", "UPDATE"},
    "liquidations": {"SELECT", "INSERT", "UPDATE"},
    "attribution_weekly": {"SELECT", "INSERT", "DELETE"},
    "fill_rate_stats": {"SELECT", "INSERT", "DELETE"},
    "fill_rate_model_artifacts": {"SELECT", "INSERT"},
}
EXPECTED_TABLES: dict[str, set[str]] = {
    **WRITTEN,
    "funding_credit_history": _R,
    "funding_trades": _R,
    "funding_interest_payments": _R,
    "attribution_legacy_offer_cells": _R,
    "venue_credit_mirror": _R,
    "submission_attempt_journal": _R,
    "transport_outcome_journal": _R,
    "execution_resolution_journal": _R,
    "funding_candles": _R,
    "funding_book_snapshots": _R,
    # The delivery metrics (migration 638455da00bb).
    "deployments": _R,
}
EXPECTED: set[tuple[str, ...]] = (
    {("table", t, p) for t, ps in EXPECTED_TABLES.items() for p in ps}
    | {("schema", "public", "USAGE")}
)


def _create_role(conn) -> None:
    conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') "
                         f"THEN CREATE ROLE {ROLE}; END IF; END $$")


def _build_worst_case(url: str) -> None:
    """Default privileges hand every new table and sequence, and the schema, to the role."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _create_role(conn)
        conn.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA public TO {ROLE}")
        conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {ROLE}")
        conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")


def _build_prod_faithful(url: str) -> None:
    """Production's host setup (fresh-host-setup §1a): no default privileges, no schema grant."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _create_role(conn)
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")


def _build_stray(url: str) -> None:
    """Grants outside the allowlist at the previous revision, as a hand-run GRANT could leave them
    (the role used to be ``bfx_bot``'s twin: ledger and trading-state writes, the archive)."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _create_role(conn)
    engine.dispose()
    alembic(url, "upgrade", _PREVIOUS)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"GRANT INSERT, UPDATE ON public.trading_state TO {ROLE}")
        conn.exec_driver_sql(f"GRANT INSERT ON public.submission_attempt_journal TO {ROLE}")
        conn.exec_driver_sql(f"GRANT UPDATE (fill_prob) ON public.fill_rate_stats TO {ROLE}")
        conn.exec_driver_sql(f"GRANT SELECT ON public.exchange_account_credentials TO {ROLE}")
        conn.exec_driver_sql(f"GRANT USAGE ON SEQUENCE public.deployments_id_seq TO {ROLE}")
        conn.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA legacy_archive TO {ROLE}")
        conn.exec_driver_sql(f"GRANT SELECT ON legacy_archive.event_log TO {ROLE}")
        signature = conn.scalar(text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY 1 LIMIT 1"))
        conn.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")


_BUILDS = {
    "research_allowlist_worst_case": _build_worst_case,
    "research_allowlist_prod_faithful": _build_prod_faithful,
    "research_allowlist_stray": _build_stray,
}


@pytest.fixture(params=sorted(_BUILDS))
def head_db(request, pg_templates, pg_clone):
    engine = create_engine(pg_clone(pg_templates.template(request.param, _BUILDS[request.param])))
    try:
        yield engine
    finally:
        engine.dispose()


def _diff(found: set[tuple[str, ...]]) -> dict[str, list[tuple[str, ...]]]:
    return {"unexpected": sorted(found - EXPECTED), "missing": sorted(EXPECTED - found)}


def test_effective_privileges_equal_the_allowlist(head_db) -> None:
    with head_db.connect() as conn:
        assert _diff(_effective(conn, ROLE)) == {"unexpected": [], "missing": []}


def test_the_role_writes_nothing_but_the_research_tables(head_db) -> None:
    """Independent of the list above: every write (INSERT, UPDATE, DELETE, TRUNCATE, table or
    column level) is on a research table, never TRUNCATE, never a sequence or an archive."""
    with head_db.connect() as conn:
        found = _effective(conn, ROLE)
    writes = {f for f in found if f[-1] in {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}}
    assert {f[1] for f in writes} <= set(WRITTEN)
    assert not any(f[-1] == "TRUNCATE" for f in found)
    assert not any(f[0] == "sequence" for f in found)
    assert not any(f[0] == "schema" and (f[1] == "legacy_archive" or f[2] == "CREATE")
                   for f in found)
    assert not any(f[1].startswith("legacy_archive.") for f in found if f[0] in {"table", "column"})


def test_the_bot_tables_stay_unwritable(head_db) -> None:
    """The ledger and trading state, by name: no write privilege of any kind, at any level."""
    bot_tables = ("trading_state", "execution_decisions", "submission_attempt_journal",
                  "transport_outcome_journal", "execution_resolution_journal", "ledger_observation",
                  "accepted_capital_basis", "quarantine_opening", "capital_policy_revisions",
                  "capital_authority_epoch", "trading_control_requests", "exchange_account_credentials",
                  "api_keys", "deployments", "database_realm")
    with head_db.connect() as conn:
        for table in bot_tables:
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert conn.scalar(text("SELECT has_table_privilege(:r, :t, :p)"),
                                   {"r": ROLE, "t": f"public.{table}", "p": privilege}) is False, \
                    (table, privilege)
            for privilege in ("INSERT", "UPDATE"):
                assert conn.scalar(text("SELECT has_any_column_privilege(:r, :t, :p)"),
                                   {"r": ROLE, "t": f"public.{table}", "p": privilege}) is False, \
                    (table, privilege)
        for table in ("exchange_account_credentials", "api_keys", "credential_rotation_requests"):
            assert conn.scalar(text("SELECT has_any_column_privilege(:r, :t, 'SELECT')"),
                               {"r": ROLE, "t": f"public.{table}"}) is False, table


def test_without_the_role_the_migration_grants_nothing() -> None:
    """The role is host configuration: where it is absent, upgrade() issues no statement."""
    spec = importlib.util.spec_from_file_location("research_role_allowlist", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with mock.patch.object(module, "_role_exists", return_value=False) as exists, \
            mock.patch.object(module, "op") as op:
        module.upgrade()
    exists.assert_called_once_with(ROLE)
    op.execute.assert_not_called()
