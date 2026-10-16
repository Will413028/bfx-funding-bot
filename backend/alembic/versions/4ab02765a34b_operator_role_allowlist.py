"""``bfx_operator``: the operator CLI's role, an exact allowlist from its first grant.

The operator CLI (``apps/operator_cli.py``, run as the one-off ``bfx-operator`` compose
service) queues the same three operator requests the web API does -- trading control,
currency enable/disable, UNKNOWN adjudication -- and reads what those requests are
judged against. It never applies one: the account daemon's workers do, re-checking the
operator under the account lock. So its role may INSERT only the request columns
(``REQUEST_COLUMNS`` of ``TradingControlRequestRow``, ``CapitalPolicyRequestRow``,
``UncertaintyResolutionRequestRow``) and SELECT what the read and validation paths of
``modules/execution/operator_commands.py`` and the ledger's operator ports read; no
UPDATE, DELETE or TRUNCATE anywhere, no sequence, no ``legacy_archive`` or ``auth``.

Like ``d0e1f2a3b4c6`` for the web API, the upgrade first revokes everything the role
holds in ``public`` (a host's default privileges or a hand-run GRANT), then grants back
``OPERATOR_TABLE_PRIVILEGES`` and ``OPERATOR_COLUMN_PRIVILEGES``.
``tests/integration/test_operator_privilege_allowlist.py`` compares the effective
privileges at head with its own copy; a later migration that changes what the role may
do updates that copy in the same change.

The role is host configuration (``docs/runbooks/fresh-host-setup.md`` §1a): where it
does not exist this revision grants nothing, and a later CLI run fails to log in.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md): revoking the grants would
take the operator CLI's path away while the role remains.

Revision ID: 4ab02765a34b
Revises: 49b37faec086
"""

from sqlalchemy import text

from alembic import op

revision = "4ab02765a34b"
down_revision = "49b37faec086"
branch_labels = None
depends_on = None
# Grants only; no table, cursor or event_log content changes.
ledger_contract = "preserved"

_ROLE = "bfx_operator"

_R = ("SELECT",)
# The observed offers an UNKNOWN is matched against (``resolver.py``'s ``_OFFER_COLUMNS``).
_OBSERVED_OFFER = (
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining", "rate",
    "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created", "mts_updated",
)

# Whole rows the ORM reads (account and membership, trading state and its cancel-all,
# the three outboxes, policy heads and revisions) and the authority epoch.
OPERATOR_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "capital_authority_epoch": _R,
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "capital_policy_revisions": _R,
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "funding_cancel_all_audit": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
}

# (table, privilege) -> columns: the ledger reads an adjudication is judged against, the
# schema head, and the request columns of each outbox. Never ``evidence``, ``raw``,
# ``scope_block`` or ``normalized_payload``.
OPERATOR_COLUMN_PRIVILEGES: dict[tuple[str, str], tuple[str, ...]] = {
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
    ("execution_resolution_journal", "SELECT"): (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    ),
    ("ledger_observation", "SELECT"): (
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest", "wallets_complete",
        "offers_complete", "credits_complete", "loans_complete", "offer_history_complete",
        "credit_history_complete", "trades_complete",
        "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
        "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
        "trades_requested_start_ms", "trades_requested_end_ms", "history_symbols",
        "first_page_counts",
    ),
    ("ledger_observation_offer", "SELECT"): _OBSERVED_OFFER,
    ("ledger_observation_offer_history", "SELECT"): (
        *_OBSERVED_OFFER, "terminal_kind", "occurred_at_ms",
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
        "match_rate", "match_period_days", "match_offer_type", "match_flags",
    ),
    ("transport_outcome_journal", "SELECT"): (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    ),
    ("venue_offer_mirror", "SELECT"): (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    ),
    ("capital_policy_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    ),
    ("trading_control_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    ),
    ("uncertainty_resolution_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "action", "observation_id", "venue_offer_id", "decision", "reason", "requested_by",
        "created_at_ms",
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
    # A host grant of CREATE on the schema would let the role add objects of its own.
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_ROLE}")
    for table, privileges in OPERATOR_TABLE_PRIVILEGES.items():
        op.execute(f"GRANT {', '.join(privileges)} ON public.{table} TO {_ROLE}")
    for (table, privilege), columns in OPERATOR_COLUMN_PRIVILEGES.items():
        op.execute(f"GRANT {privilege} ({', '.join(columns)}) ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
