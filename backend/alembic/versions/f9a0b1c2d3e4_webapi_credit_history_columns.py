"""Web API execution history: column grants on the observed credit history.

Under the ledger the operator's execution history shows the venue's credit ends after the
switch (``CREDIT_CLOSED``) from ``ledger_observation_credit_history``, which ``bfx_webapi``
could not read. It is granted exactly the columns that history names (``CREDIT_END_COLUMNS``);
never ``raw``. Fills need nothing new: ``e1f2a3b4c5d7`` already grants the offer history's
match columns, ``terminal_kind`` and ``occurred_at_ms``.

Allowlist: ``WEBAPI_TABLE_PRIVILEGES`` / ``WEBAPI_COLUMN_PRIVILEGES`` below are the newest
copy of the exact allowlist ``d0e1f2a3b4c6`` introduced (its docstring says how it is kept);
``tests/integration/test_webapi_privilege_allowlist.py`` compares the effective privileges at
head with this copy. Downgrade revokes only what this revision granted.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

from sqlalchemy import text

from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLE = "bfx_webapi"

ATTEMPT_MATCH_COLUMNS = ("match_rate", "match_period_days", "match_offer_type", "match_flags")
OBSERVATION_MATCH_COLUMNS = (
    "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
    "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
    "trades_requested_start_ms", "trades_requested_end_ms", "history_symbols", "first_page_counts",
)
OBSERVED_OFFER_MATCH_COLUMNS = (
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining",
    "rate", "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created",
    "mts_updated",
)

CREDIT_END_COLUMNS = (
    "observation_id", "venue_credit_id", "source_kind", "symbol", "amount", "rate",
    "terminal_kind", "occurred_at_ms",
)

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
        *OBSERVATION_MATCH_COLUMNS,
    ),
    ("ledger_observation_credit_history", "SELECT"): CREDIT_END_COLUMNS,
    ("ledger_observation_offer", "SELECT"): OBSERVED_OFFER_MATCH_COLUMNS,
    ("ledger_observation_offer_history", "SELECT"): (
        *OBSERVED_OFFER_MATCH_COLUMNS, "terminal_kind", "occurred_at_ms",
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
        *ATTEMPT_MATCH_COLUMNS,
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


# What this revision grants beyond ``e1f2a3b4c5d7``'s allowlist.
_GRANTED: dict[tuple[str, str], tuple[str, ...]] = {
    ("ledger_observation_credit_history", "SELECT"): CREDIT_END_COLUMNS,
}


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    if _role_exists(_ROLE):
        for (table, privilege), columns in _GRANTED.items():
            op.execute(f"GRANT {privilege} ({', '.join(columns)}) ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    if _role_exists(_ROLE):
        for (table, privilege), columns in _GRANTED.items():
            op.execute(f"REVOKE {privilege} ({', '.join(columns)}) ON public.{table} FROM {_ROLE}")
