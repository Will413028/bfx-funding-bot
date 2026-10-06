"""``bfx_webapi``'s privileges in ``public`` and ``legacy_archive`` are an exact allowlist.

``EXPECTED_*`` below is that allowlist at head, written out here and checked against the
EFFECTIVE privileges of three builds (worst-case default privileges, production's host setup,
stray grants at an earlier revision): a migration that grants or revokes anything for the web
API must update it in the same change, or ``test_effective_privileges_equal_the_allowlist``
fails. Each downgrade step must restore the previous revision's allowlist exactly
(``test_round_trip_keeps_the_allowlist``): ``PRE_CLOSE_COLUMNS`` is the one before
e4f5a6b7c8d9 revoked the pre-switch evidence column (``CLOSED_COLUMNS``); ``PRE_ARCHIVE_TABLES``
the table list before c2d3e4f5a6b7 archived five of them (``ARCHIVED_READS``; at head the web
API reads only ``ARCHIVE_COLUMNS`` of ``legacy_archive.event_log`` there); ``MATCH_COLUMNS``
the one e1f2a3b4c5d7 left, which a downgrade of f9a0b1c2d3e4 restores; ``PREVIOUS_*`` the one
d0e1f2a3b4c6 left, which a downgrade of e1f2a3b4c5d7 restores.

Convention (since e4f5a6b7c8d9): a migration states only the grants it changes. It no longer
carries a ``WEBAPI_*`` copy of the whole allowlist, and no test compares the newest migration's
copy with this one: the effective privileges at head and after each downgrade step are the
check. The copies d0e1f2a3b4c6 through c2d3e4f5a6b7 carry stay as they were (their upgrades
and downgrades still use them).

Mutation checks (one at a time; revert after each):

* Drop a table from c2d3e4f5a6b7's ``WEBAPI_TABLE_PRIVILEGES``: the effective-privilege test fails.
* Add ``execution_decisions`` SELECT: the same test, and ``test_execution_decisions_are_unreadable``.
* Widen a column grant to the whole table: the effective-privilege test fails.
* Remove the REVOKE steps from d0e1f2a3b4c6's ``upgrade``: the effective-privilege test fails
  on the worst-case and stray builds.
* Grant ``raw`` on ``ledger_observation_credit_history`` in f9a0b1c2d3e4: the effective-privilege
  and payload-column tests fail.
* Skip c2d3e4f5a6b7's revoke, or grant ``account_id`` of the archived event log: the
  effective-privilege test fails (table reads of the archive, or a column too many).
* Skip e4f5a6b7c8d9's revoke, or its grant back on downgrade: the effective-privilege or the
  round-trip test fails.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from tests.pg_templates import alembic

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_PREVIOUS = "c9d0e1f2a3b5"
_PREVIOUS_HEAD = "d0e1f2a3b4c6"
_MATCH_HEAD = "e8f9a0b1c2d3"  # e1f2a3b4c5d7's allowlist, unchanged up to here
_PRE_ARCHIVE = "b1c2d3e4f5a6"  # f9a0b1c2d3e4's allowlist, unchanged up to here
_PRE_CLOSE = "d3e4f5a6b7c8"  # c2d3e4f5a6b7's allowlist, unchanged up to here
_ARCHIVE = "legacy_archive"
_CREDIT_ENDS = ("ledger_observation_credit_history", "SELECT")
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
    "exchange_account_credentials": {"INSERT", "SELECT", "UPDATE"},
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "funding_cancel_all_audit": _R,
    "funding_candles": _R,
    "funding_credit_history": _R,
    "funding_interest_payments": _R,
    "funding_trades": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
    "user_configs": _RW,
    "user_profiles": {"INSERT", "SELECT", "UPDATE"},
}
# Read in public until c2d3e4f5a6b7 moved them to legacy_archive.
ARCHIVED_READS: dict[str, set[str]] = {
    "event_log": _R, "execution_uncertainties": _R, "offer_claims": _R, "position_state": _R,
    "submission_attempts": _R,
}
PRE_ARCHIVE_TABLES = EXPECTED_TABLES | ARCHIVED_READS
# The archived execution history's read (modules.execution.archived_execution_history).
ARCHIVE_COLUMNS: dict[tuple[str, str], set[str]] = {
    ("event_log", "SELECT"): {
        "event_seq", "exchange_account_id", "deployment_environment", "event_type",
        "occurred_at_ms", "venue_offer_id", "cid", "payload",
    },
}
_OBSERVED_OFFER = {
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining", "rate",
    "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created", "mts_updated",
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
        "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
        "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
        "trades_requested_start_ms", "trades_requested_end_ms", "history_symbols",
        "first_page_counts",
    },
    _CREDIT_ENDS: {
        "observation_id", "venue_credit_id", "source_kind", "symbol", "amount", "rate",
        "terminal_kind", "occurred_at_ms",
    },
    ("ledger_observation_offer", "SELECT"): _OBSERVED_OFFER,
    ("ledger_observation_offer_history", "SELECT"): {
        *_OBSERVED_OFFER, "terminal_kind", "occurred_at_ms",
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
        "match_rate", "match_period_days", "match_offer_type", "match_flags",
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
        "observation_id",
    },
    ("venue_offer_mirror", "SELECT"): {
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    },
}
# Revoked by e4f5a6b7c8d9: the pre-switch evidence column of an uncertainty request.
CLOSED_COLUMNS: dict[tuple[str, str], set[str]] = {
    ("uncertainty_resolution_requests", "INSERT"): {"reconcile_event_seq"},
}
PRE_CLOSE_COLUMNS = {key: cols | CLOSED_COLUMNS.get(key, set())
                     for key, cols in EXPECTED_COLUMNS.items()}
PREVIOUS_COLUMNS: dict[tuple[str, str], set[str]] = {
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
# What the web API holds in schemas public and legacy_archive: tables, columns, sequences,
# functions, schemas. An archived relation is named ``legacy_archive.<name>``.
def _held(columns: dict[tuple[str, str], set[str]], *, archived: bool = False) -> set[tuple[str, ...]]:
    tables = EXPECTED_TABLES if archived else PRE_ARCHIVE_TABLES
    return (
        {("table", t, p) for t, ps in tables.items() for p in ps}
        | {("column", t, c, p) for (t, p), cs in columns.items() for c in cs}
        | {("schema", "public", "USAGE")}
        | ({("column", f"{_ARCHIVE}.{t}", c, p) for (t, p), cs in ARCHIVE_COLUMNS.items()
            for c in cs} | {("schema", _ARCHIVE, "USAGE")} if archived else set())
    )


# e1f2a3b4c5d7's allowlist: c2d3e4f5a6b7's without f9a0b1c2d3e4's credit-history grant.
MATCH_COLUMNS = {key: cols for key, cols in PRE_CLOSE_COLUMNS.items() if key != _CREDIT_ENDS}
EXPECTED = _held(EXPECTED_COLUMNS, archived=True)
PRE_CLOSE = _held(PRE_CLOSE_COLUMNS, archived=True)
PRE_ARCHIVE = _held(PRE_CLOSE_COLUMNS)
MATCH = _held(MATCH_COLUMNS)
PREVIOUS = _held(PREVIOUS_COLUMNS)

_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_SCHEMAS = ("public", _ARCHIVE)
_RELATIONS = """SELECT c.oid, CASE n.nspname WHEN 'public' THEN c.relname
                ELSE n.nspname || '.' || c.relname END
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname IN ('public', 'legacy_archive')
                AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY 2"""


def _effective(conn) -> set[tuple[str, ...]]:
    """Everything ``bfx_webapi`` can do in ``public`` and ``legacy_archive``, through the privilege functions
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
    for (oid, name) in conn.exec_driver_sql(
            "SELECT c.oid, CASE n.nspname WHEN 'public' THEN c.relname "
            "ELSE n.nspname || '.' || c.relname END FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('public', 'legacy_archive') AND c.relkind = 'S'"):
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            if conn.scalar(text("SELECT has_sequence_privilege('bfx_webapi', :s, :p)"),
                           {"s": oid, "p": privilege}):
                found.add(("sequence", name, privilege))
    # PUBLIC may execute any function by default; only the role's own grants are the allowlist's business.
    for (signature,) in conn.exec_driver_sql(
            "SELECT p.oid::regprocedure::text FROM pg_proc p, aclexplode(p.proacl) a "
            "WHERE p.pronamespace = 'public'::regnamespace "
            "AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = 'bfx_webapi')"):
        found.add(("function", signature, "EXECUTE"))
    for schema in _SCHEMAS:
        if conn.scalar(text("SELECT to_regnamespace(:n) IS NULL"), {"n": schema}):
            continue
        for privilege in ("USAGE", "CREATE"):
            if conn.scalar(text("SELECT has_schema_privilege('bfx_webapi', :n, :p)"),
                           {"n": schema, "p": privilege}):
                found.add(("schema", schema, privilege))
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


def _diff(
    found: set[tuple[str, ...]], expected: set[tuple[str, ...]] = EXPECTED
) -> dict[str, list[tuple[str, ...]]]:
    return {"unexpected": sorted(found - expected), "missing": sorted(expected - found)}


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
    # e4f5a6b7c8d9's downgrade restores c2d3e4f5a6b7's allowlist exactly...
    alembic(url, "downgrade", _PRE_CLOSE)
    with engine.connect() as conn:
        assert _diff(_effective(conn), PRE_CLOSE) == {"unexpected": [], "missing": []}
    # ...c2d3e4f5a6b7's restores f9a0b1c2d3e4's...
    alembic(url, "downgrade", _PRE_ARCHIVE)
    with engine.connect() as conn:
        assert _diff(_effective(conn), PRE_ARCHIVE) == {"unexpected": [], "missing": []}
    # ...f9a0b1c2d3e4's restores e1f2a3b4c5d7's...
    alembic(url, "downgrade", _MATCH_HEAD)
    with engine.connect() as conn:
        assert _diff(_effective(conn), MATCH) == {"unexpected": [], "missing": []}
    # ...e1f2a3b4c5d7's restores d0e1f2a3b4c6's...
    alembic(url, "downgrade", _PREVIOUS_HEAD)
    with engine.connect() as conn:
        assert _diff(_effective(conn), PREVIOUS) == {"unexpected": [], "missing": []}
    # ...and d0e1f2a3b4c6's own downgrade leaves it alone.
    alembic(url, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        assert _diff(_effective(conn), PREVIOUS) == {"unexpected": [], "missing": []}
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with engine.connect() as conn:
        assert _diff(_effective(conn)) == {"unexpected": [], "missing": []}


def test_the_match_grants_never_reach_the_payload_columns(head_db) -> None:
    """Whatever else the allowlist grows by, these stay ungranted (effective, not just listed)."""
    _, engine, _ = head_db
    never = (
        ("ledger_observation_offer", "raw"), ("ledger_observation_offer_history", "raw"),
        ("ledger_observation_credit_history", "raw"),
        ("ledger_observation", "evidence"), ("submission_attempt_journal", "normalized_payload"),
        ("submission_attempt_journal", "payload_sha256"),
        ("accepted_capital_basis", "scope_block"),
    )
    with engine.connect() as conn:
        for table, column in never:
            assert conn.scalar(
                text("SELECT has_column_privilege('bfx_webapi', :t, :c, 'SELECT')"),
                {"t": f"public.{table}", "c": column},
            ) is False, (table, column)
        assert not any(c in {"raw", "evidence", "normalized_payload", "scope_block"}
                       for (t, p), cols in EXPECTED_COLUMNS.items() for c in cols), "allowlist names a payload column"

