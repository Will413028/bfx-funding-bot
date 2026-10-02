"""``bfx_webapi``'s privileges in schema ``public`` become one exact, migration-owned allowlist.

Until now the web API's grants were the sum of many migrations plus three psql
runbook steps, so nothing said what it may *not* read: a table no migration
revoked stayed readable wherever default privileges handed it out, and a test
running "as bfx_webapi" could not tell a read inside the allowlist from one
outside it. This revokes everything ``bfx_webapi`` holds in ``public`` (tables,
column-level grants, sequences, functions) and grants back exactly
``WEBAPI_TABLE_PRIVILEGES`` and ``WEBAPI_COLUMN_PRIVILEGES``, plus
``USAGE ON SCHEMA public``. Any later migration that grants the web API
something must add it to a copy of these constants;
``tests/integration/test_webapi_privilege_allowlist.py`` compares the effective
privileges at head with the newest copy and fails when they differ.

The allowlist is what production's ``bfx_webapi`` holds today. Everything the
migration chain grants is in it; three legacy tables were granted by the
``psql`` runbook steps named in their migrations and are in production, so they
are in it too (the web API's config, API-key and profile endpoints use them):
``api_keys`` and ``user_configs`` (SELECT, INSERT, UPDATE, DELETE) and
``user_profiles`` UPDATE. Nothing production holds is dropped.

Out of scope: ``release_archive`` (the web API has no USAGE on that schema), the
default privileges (host configuration) and the other roles.

Downgrade leaves the privileges alone. The state before this revision is "the
chain's grants, plus whatever the host's defaults and runbook steps added", and
production's version of it is exactly this allowlist; revoking it would take
the web API's reads away, and the earlier revisions' own downgrades revoke what
they granted.

Revision ID: d0e1f2a3b4c6
Revises: c9d0e1f2a3b5
"""

from sqlalchemy import text

from alembic import op

revision = "d0e1f2a3b4c6"
down_revision = "c9d0e1f2a3b5"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLE = "bfx_webapi"

_RW = ("DELETE", "INSERT", "SELECT", "UPDATE")
_R = ("SELECT",)

WEBAPI_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "account_config_drafts": _RW,
    "api_keys": _RW,
    "attribution_weekly": _R,
    "capital_authority_epoch": _R,
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "capital_policy_revisions": _R,
    "deployments": _R,
    "event_log": _R,
    "exchange_account_credentials": ("INSERT", "SELECT", "UPDATE"),
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
    "user_profiles": ("INSERT", "SELECT", "UPDATE"),
}

# (table, privilege) -> columns. Only columns whose table has no table-level grant of
# that privilege; never ``evidence``, ``raw``, ``scope_block`` or ``normalized_payload``.
WEBAPI_COLUMN_PRIVILEGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("accepted_capital_basis", "SELECT"): (
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    ),
    ("accepted_capital_basis_attempt", "SELECT"): (
        "basis_id", "attempt_id", "symbol", "classification",
    ),
    ("accepted_capital_basis_credit", "SELECT"): ("basis_id", "symbol"),
    ("accepted_capital_basis_quarantine", "SELECT"): ("basis_id", "quarantine_id"),
    ("accepted_capital_basis_symbol", "SELECT"): (
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    ),
    ("alembic_version", "SELECT"): ("version_num",),
    ("capital_policy_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    ),
    ("execution_resolution_journal", "SELECT"): (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    ),
    ("ledger_observation", "SELECT"): (
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest",
        "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
        "offer_history_complete", "credit_history_complete", "trades_complete",
    ),
    ("ledger_observation_query", "SELECT"): (
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    ),
    ("quarantine_opening", "SELECT"): (
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    ),
    ("submission_attempt_journal", "SELECT"): (
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
    ),
    ("trading_control_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    ),
    ("transport_outcome_journal", "SELECT"): (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    ),
    ("uncertainty_resolution_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "venue_offer_id", "action", "decision", "reason", "requested_by", "created_at_ms",
        "reconcile_event_seq", "observation_id",
    ),
    ("venue_offer_mirror", "SELECT"): (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    ),
}


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    if not _role_exists(_ROLE):
        return
    # Table-level REVOKE also removes the role's column-level grants of that privilege.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {_ROLE}")
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_ROLE}")
    for table, privileges in WEBAPI_TABLE_PRIVILEGES.items():
        op.execute(f"GRANT {', '.join(privileges)} ON public.{table} TO {_ROLE}")
    for (table, privilege), columns in WEBAPI_COLUMN_PRIVILEGES.items():
        op.execute(f"GRANT {privilege} ({', '.join(columns)}) ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    """Privileges stay as they are (see the module docstring)."""
