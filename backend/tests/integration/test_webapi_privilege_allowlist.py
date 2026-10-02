"""d0e1f2a3b4c6: ``bfx_webapi``'s privileges in ``public`` are an exact allowlist.

``EXPECTED_*`` below is written out here, not imported from the migration: the
test is the second opinion. A later migration that grants the web API anything
must update this list in the same change, or ``test_effective_privileges_equal_the_allowlist``
fails.

Mutation checks (one at a time; revert after each):

* Drop a table from ``WEBAPI_TABLE_PRIVILEGES``: the effective-privilege and the
  migration-constants tests fail.
* Add ``execution_decisions`` SELECT: the same tests, and ``test_execution_decisions_are_unreadable``.
* Widen a column grant to the whole table: the effective-privilege test fails.
* Remove the REVOKE steps from ``upgrade``: the worst-case build and
  ``test_stray_grants_are_revoked`` fail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from tests.pg_templates import alembic

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic/versions"
_PREVIOUS = "c9d0e1f2a3b5"
_RW = {"DELETE", "INSERT", "SELECT", "UPDATE"}
_R = {"SELECT"}

EXPECTED_TABLES: dict[str, set[str]] = {
    "account_config_drafts": _RW,
    "api_keys": _RW,
    "attribution_weekly": _R,
    "capital_authority_epoch": _R,
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "capital_policy_revisions": _R,
    "deployments": _R,
    "event_log": _R,
    "exchange_account_credentials": {"INSERT", "SELECT", "UPDATE"},
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "execution_uncertainties": _R,
    "funding_cancel_all_audit": _R,
    "funding_candles": _R,
    "funding_credit_history": _R,
    "funding_interest_payments": _R,
    "funding_trades": _R,
    "offer_claims": _R,
    "position_state": _R,
    "submission_attempts": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
    "user_configs": _RW,
    "user_profiles": {"INSERT", "SELECT", "UPDATE"},
}
EXPECTED_COLUMNS: dict[tuple[str, str], set[str]] = {
    ("accepted_capital_basis", "SELECT"): {
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    },
    ("accepted_capital_basis_attempt", "SELECT"): {
        "basis_id", "attempt_id", "symbol", "classification",
    },
    ("accepted_capital_basis_credit", "SELECT"): {"basis_id", "symbol"},
    ("accepted_capital_basis_quarantine", "SELECT"): {"basis_id", "quarantine_id"},
    ("accepted_capital_basis_symbol", "SELECT"): {
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    },
    ("alembic_version", "SELECT"): {"version_num"},
    ("capital_policy_requests", "INSERT"): {
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    },
    ("execution_resolution_journal", "SELECT"): {
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    },
    ("ledger_observation", "SELECT"): {
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest", "wallets_complete",
        "offers_complete", "credits_complete", "loans_complete", "offer_history_complete",
        "credit_history_complete", "trades_complete",
    },
    ("ledger_observation_query", "SELECT"): {
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    },
    ("quarantine_opening", "SELECT"): {
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    },
    ("submission_attempt_journal", "SELECT"): {
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
    },
    ("trading_control_requests", "INSERT"): {
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    },
    ("transport_outcome_journal", "SELECT"): {
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    },
    ("uncertainty_resolution_requests", "INSERT"): {
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "venue_offer_id", "action", "decision", "reason", "requested_by", "created_at_ms",
        "reconcile_event_seq", "observation_id",
    },
    ("venue_offer_mirror", "SELECT"): {
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    },
}
# What the web API holds in schema public: tables, columns, sequences, functions, schema.
EXPECTED: set[tuple[str, ...]] = (
    {("table", t, p) for t, ps in EXPECTED_TABLES.items() for p in ps}
    | {("column", t, c, p) for (t, p), cs in EXPECTED_COLUMNS.items() for c in cs}
    | {("schema", "public", "USAGE")}
)

_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_RELATIONS = """SELECT c.oid, c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
                AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY c.relname"""


def _effective(conn) -> set[tuple[str, ...]]:
    """Everything ``bfx_webapi`` can do in ``public``, through the privilege functions
    (so grants via PUBLIC or role membership count, not only its own ACL entries)."""
    found: set[tuple[str, ...]] = set()
    for oid, name in conn.exec_driver_sql(_RELATIONS).all():
        held = set()
        for privilege in _TABLE_PRIVILEGES:
            if conn.scalar(text("SELECT has_table_privilege('bfx_webapi', :o, :p)"),
                           {"o": oid, "p": privilege}):
                held.add(privilege)
                found.add(("table", name, privilege))
        columns = conn.exec_driver_sql(
            f"SELECT attname FROM pg_attribute WHERE attrelid = {oid} AND attnum > 0 "
            "AND NOT attisdropped").scalars().all()
        for privilege in _COLUMN_PRIVILEGES:
            if privilege in held:
                continue
            for column in columns:
                if conn.scalar(text("SELECT has_column_privilege('bfx_webapi', :o, :c, :p)"),
                               {"o": oid, "c": column, "p": privilege}):
                    found.add(("column", name, column, privilege))
    for (name,) in conn.exec_driver_sql(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relkind = 'S'"):
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            if conn.scalar(text("SELECT has_sequence_privilege('bfx_webapi', :s, :p)"),
                           {"s": f"public.{name}", "p": privilege}):
                found.add(("sequence", name, privilege))
    # PUBLIC may execute any function by default; only the role's own grants are the allowlist's business.
    for (signature,) in conn.exec_driver_sql(
            "SELECT p.oid::regprocedure::text FROM pg_proc p, aclexplode(p.proacl) a "
            "WHERE p.pronamespace = 'public'::regnamespace "
            "AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = 'bfx_webapi')"):
        found.add(("function", signature, "EXECUTE"))
    for privilege in ("USAGE", "CREATE"):
        if conn.scalar(text("SELECT has_schema_privilege('bfx_webapi', 'public', :p)"),
                       {"p": privilege}):
            found.add(("schema", "public", privilege))
    return found


def _build_worst_case(url: str) -> None:
    """``_reset``'s worst case: default privileges hand every new table and sequence to the role."""
    engine = create_engine(url)
    _reset(engine)
    engine.dispose()
    alembic(url, "upgrade", "head")


def _build_prod_faithful(url: str) -> None:
    """Production's host setup: no default privileges and no schema grant for the web API."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_webapi")
        conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM bfx_webapi")
        conn.exec_driver_sql("REVOKE ALL ON SCHEMA public FROM bfx_webapi")
    engine.dispose()
    alembic(url, "upgrade", "head")


def _build_stray(url: str) -> None:
    """Grants outside the allowlist that exist at the previous revision (column, sequence,
    function, table), as a host's hand-run GRANT could leave them."""
    engine = create_engine(url)
    _reset(engine)
    engine.dispose()
    alembic(url, "upgrade", _PREVIOUS)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bfx_webapi")
        conn.exec_driver_sql("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM bfx_webapi")
        conn.exec_driver_sql("GRANT SELECT (evidence) ON public.ledger_observation TO bfx_webapi")
        conn.exec_driver_sql("GRANT UPDATE (symbol) ON public.venue_offer_mirror TO bfx_webapi")
        conn.exec_driver_sql("GRANT SELECT ON public.execution_decisions TO bfx_webapi")
        conn.exec_driver_sql("GRANT USAGE ON SEQUENCE public.deployments_id_seq TO bfx_webapi")
        signature = conn.scalar(text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY 1 LIMIT 1"))
        conn.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO bfx_webapi")
    engine.dispose()
    alembic(url, "upgrade", "head")


_BUILDS = {
    "webapi_allowlist_worst_case": _build_worst_case,
    "webapi_allowlist_prod_faithful": _build_prod_faithful,
    "webapi_allowlist_stray": _build_stray,
}


@pytest.fixture(params=sorted(_BUILDS))
def head_db(request, pg_templates, pg_clone):
    url = pg_clone(pg_templates.template(request.param, _BUILDS[request.param]))
    engine = create_engine(url)
    try:
        yield url, engine, request.param
    finally:
        engine.dispose()


def _diff(found: set[tuple[str, ...]]) -> dict[str, list[tuple[str, ...]]]:
    return {"unexpected": sorted(found - EXPECTED), "missing": sorted(EXPECTED - found)}


def test_effective_privileges_equal_the_allowlist(head_db) -> None:
    _, engine, _ = head_db
    with engine.connect() as conn:
        assert _diff(_effective(conn)) == {"unexpected": [], "missing": []}


def test_execution_decisions_are_unreadable(head_db) -> None:
    _, engine, _ = head_db
    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT has_table_privilege('bfx_webapi', 'public.execution_decisions', 'SELECT')")) is False
    with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql("SELECT count(*) FROM public.execution_decisions")


def test_round_trip_keeps_the_allowlist(head_db) -> None:
    url, engine, _ = head_db
    alembic(url, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        assert _diff(_effective(conn)) == {"unexpected": [], "missing": []}
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with engine.connect() as conn:
        assert _diff(_effective(conn)) == {"unexpected": [], "missing": []}


def test_the_migration_constants_are_the_allowlist() -> None:
    spec = importlib.util.spec_from_file_location(
        "webapi_allowlist_migration", _VERSIONS / "d0e1f2a3b4c6_webapi_privileges_exact_allowlist.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {t: set(p) for t, p in module.WEBAPI_TABLE_PRIVILEGES.items()} == EXPECTED_TABLES
    assert {k: set(c) for k, c in module.WEBAPI_COLUMN_PRIVILEGES.items()} == EXPECTED_COLUMNS
    assert all(len(set(c)) == len(c) for c in module.WEBAPI_COLUMN_PRIVILEGES.values())
