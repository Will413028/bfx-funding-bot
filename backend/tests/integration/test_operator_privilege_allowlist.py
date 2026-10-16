"""``bfx_operator``'s privileges in ``public`` and ``legacy_archive`` are an exact allowlist.

``EXPECTED_*`` is head's allowlist for the operator CLI's role (``4ab02765a34b``, then
``1db83367918c`` for credential rotation): SELECT on what the CLI's read and validation paths
read, INSERT on exactly the request columns of the four operator outboxes, nothing else. It is checked against the EFFECTIVE privileges of three builds
(worst-case default privileges, production's host setup, stray grants at the previous revision): a
migration that grants or revokes anything for the role must update ``EXPECTED_*`` in the same
change. The INSERT columns are each model's ``REQUEST_COLUMNS``, so a request column added to a
model without a grant fails here too.

The role is created here, not in the shared ``_reset``: every other template would otherwise be
rebuilt with a role it never uses.

Mutation checks (one at a time; revert after each):

* Grant ``bfx_operator`` UPDATE on ``trading_state`` in ``4ab02765a34b``: the effective-privilege
  and no-write tests fail.
* Remove the REVOKE steps from its ``upgrade``: the effective-privilege test fails on the
  worst-case and stray builds.
* Drop ``observation_id`` from its uncertainty request INSERT: the effective-privilege test fails.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine, text

from bfx_funding_bot.modules.accounts.rotation_tables import CredentialRotationRequestRow
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from tests.pg_templates import alembic

from .test_trading_state_migration import _reset
from .test_webapi_privilege_allowlist import _effective

pytestmark = pytest.mark.integration

ROLE = "bfx_operator"
_REVISION = "4ab02765a34b"
_PREVIOUS = "49b37faec086"
_MIGRATION = Path(__file__).parents[2] / "alembic" / "versions" / f"{_REVISION}_operator_role_allowlist.py"
_R = {"SELECT"}
_OBSERVED_OFFER = {
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining", "rate",
    "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created", "mts_updated",
}

EXPECTED_TABLES: dict[str, set[str]] = {
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "funding_cancel_all_audit": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
    # 1db83367918c: the recipient keys the CLI seals a new credential to.
    "credential_rotation_recipients": _R,
}
EXPECTED_COLUMNS: dict[tuple[str, str], set[str]] = {
    ("capital_authority_epoch", "SELECT"): {"epoch_seq", "authority", "actor"},
    ("capital_policy_revisions", "SELECT"): {
        "id", "exchange_account_id", "deployment_environment", "symbol", "revision",
        "schema_version", "policy", "digest",
    },
    ("accepted_capital_basis", "SELECT"): {
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water",
    },
    ("accepted_capital_basis_attempt", "SELECT"): {
        "basis_id", "attempt_id", "symbol", "classification",
    },
    ("accepted_capital_basis_quarantine", "SELECT"): {"basis_id", "quarantine_id"},
    ("alembic_version", "SELECT"): {"version_num"},
    ("execution_resolution_journal", "SELECT"): {
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "actor_kind", "actor_id", "resolved_at_ms", "reason",
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
    ("transport_outcome_journal", "SELECT"): {"attempt_id", "kind", "reason", "completed_at_ms"},
    ("capital_policy_requests", "INSERT"): set(CapitalPolicyRequestRow.REQUEST_COLUMNS),
    ("trading_control_requests", "INSERT"): set(TradingControlRequestRow.REQUEST_COLUMNS),
    ("uncertainty_resolution_requests", "INSERT"): set(
        UncertaintyResolutionRequestRow.REQUEST_COLUMNS),
    # 1db83367918c: a rotation request in, and everything about it back but its sealed key;
    # of the credentials and runs only what the CLI's checks and its view name.
    ("credential_rotation_requests", "INSERT"): set(CredentialRotationRequestRow.REQUEST_COLUMNS),
    ("credential_rotation_requests", "SELECT"): (
        set(CredentialRotationRequestRow.REQUEST_COLUMNS)
        | set(CredentialRotationRequestRow.WORKER_COLUMNS)) - {"sealed_credential"},
    ("exchange_account_credentials", "SELECT"): {
        "id", "exchange_account_id", "venue", "label", "lifecycle_status", "verified_at",
        "created_at", "operator_request_id",
    },
    ("bot_runs", "SELECT"): {
        "run_id", "exchange_account_id", "deployment_environment", "started_at_ms",
        "end_recorded_at_ms",
    },
}
EXPECTED: set[tuple[str, ...]] = (
    {("table", t, p) for t, ps in EXPECTED_TABLES.items() for p in ps}
    | {("column", t, c, p) for (t, p), cs in EXPECTED_COLUMNS.items() for c in cs}
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
    """Grants outside the allowlist at the previous revision, as a hand-run GRANT could leave them."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        _create_role(conn)
    engine.dispose()
    alembic(url, "upgrade", _PREVIOUS)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"GRANT SELECT (evidence) ON public.ledger_observation TO {ROLE}")
        conn.exec_driver_sql(f"GRANT UPDATE (state) ON public.trading_control_requests TO {ROLE}")
        conn.exec_driver_sql(f"GRANT SELECT ON public.deployments TO {ROLE}")
        conn.exec_driver_sql(f"GRANT USAGE ON SEQUENCE public.deployments_id_seq TO {ROLE}")
        signature = conn.scalar(text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') "
            "ORDER BY 1 LIMIT 1"))
        conn.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")


_BUILDS = {
    "operator_allowlist_worst_case": _build_worst_case,
    "operator_allowlist_prod_faithful": _build_prod_faithful,
    "operator_allowlist_stray": _build_stray,
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


def test_the_role_writes_nothing_but_request_columns(head_db) -> None:
    """Independent of the list above: no UPDATE, DELETE or TRUNCATE on any relation, and
    INSERT only on the four outboxes."""
    with head_db.connect() as conn:
        found = _effective(conn, ROLE)
    writes = {f for f in found if f[-1] in {"UPDATE", "DELETE", "TRUNCATE"}}
    inserts = {f[1] for f in found if f[-1] == "INSERT"}
    assert writes == set()
    assert inserts <= {"capital_policy_requests", "trading_control_requests",
                       "uncertainty_resolution_requests", "credential_rotation_requests"}
    assert not any(f[0] == "schema" and f[1] == "legacy_archive" for f in found)


def test_the_payload_columns_stay_unreadable(head_db) -> None:
    never = (
        ("ledger_observation", "evidence"), ("submission_attempt_journal", "normalized_payload"),
        ("accepted_capital_basis", "scope_block"), ("ledger_observation_offer", "raw"),
        ("ledger_observation_offer_history", "raw"), ("capital_authority_epoch", "evidence"),
        ("capital_policy_revisions", "source"),
        ("credential_rotation_requests", "sealed_credential"),
        ("exchange_account_credentials", "api_key"),
        ("exchange_account_credentials", "secret_ciphertext"),
        ("exchange_account_credentials", "secret_nonce"),
        ("exchange_account_credentials", "wrapped_dek"),
        ("exchange_account_credentials", "dek_nonce"),
        ("exchange_account_credentials", "last_verify_error"),
    )
    with head_db.connect() as conn:
        for table, column in never:
            assert conn.scalar(
                text("SELECT has_column_privilege(:r, :t, :c, 'SELECT')"),
                {"r": ROLE, "t": f"public.{table}", "c": column},
            ) is False, (table, column)


def test_without_the_role_the_migration_grants_nothing() -> None:
    """The role is host configuration: where it is absent, upgrade() issues no statement."""
    spec = importlib.util.spec_from_file_location("operator_role_allowlist", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with mock.patch.object(module, "_role_exists", return_value=False) as exists, \
            mock.patch.object(module, "op") as op:
        module.upgrade()
    exists.assert_called_once_with(ROLE)
    op.execute.assert_not_called()
