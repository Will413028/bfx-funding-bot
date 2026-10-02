"""Column-level ledger read grants for the web API, a generated attempt amount, a resolved-scope index.

The web API serves operator reads from the ledger once the authority switches, but
holds no ledger privilege today. This grants ``bfx_webapi`` SELECT on an explicit
column allowlist per table: never ``evidence``, ``raw``, ``scope_block`` or
``normalized_payload``. ``submission_attempt_journal.intended_amount`` is a stored
generated column so the intended amount is readable without granting the payload.
``execution_resolution_journal`` gains the scope/time index the resolved-uncertainty
list reads through. Schema and grants only: no write path changes.

Revision ID: c9d0e1f2a3b5
Revises: b8c9d0e1f2a4
"""

from sqlalchemy import text

from alembic import op

revision = "c9d0e1f2a3b5"
down_revision = "b8c9d0e1f2a4"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ATTEMPT = "submission_attempt_journal"
_AMOUNT_CHECK = "ck_submission_attempt_intended_amount"
_RESOLVED_INDEX = "ix_execution_resolution_scope_resolved"
_ROLE = "bfx_webapi"

WEBAPI_LEDGER_COLUMNS: dict[str, tuple[str, ...]] = {
    "ledger_observation_query": (
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    ),
    "ledger_observation": (
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest",
        "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
        "offer_history_complete", "credit_history_complete", "trades_complete",
    ),
    "accepted_capital_basis": (
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    ),
    "accepted_capital_basis_attempt": ("basis_id", "attempt_id", "symbol", "classification"),
    "accepted_capital_basis_quarantine": ("basis_id", "quarantine_id"),
    "accepted_capital_basis_symbol": (
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    ),
    "accepted_capital_basis_credit": ("basis_id", "symbol"),
    _ATTEMPT: (
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
    ),
    "transport_outcome_journal": (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    ),
    "quarantine_opening": (
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    ),
    "execution_resolution_journal": (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    ),
    "venue_offer_mirror": (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    ),
}

# Amount text a numeric column can hold and the check accepts: finite and >= 0.
_BAD_AMOUNT = """
    SELECT count(*) FROM public.submission_attempt_journal j
    WHERE j.normalized_payload->>'amount' IS NULL
       OR NOT pg_input_is_valid(j.normalized_payload->>'amount', 'numeric')
       OR CASE WHEN pg_input_is_valid(j.normalized_payload->>'amount', 'numeric')
               THEN NOT ((j.normalized_payload->>'amount')::numeric >= 0
                         AND (j.normalized_payload->>'amount')::numeric < 'Infinity'::numeric)
               ELSE true END
"""


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    bad = op.get_bind().execute(text(_BAD_AMOUNT)).scalar()
    if bad:
        raise RuntimeError(
            f"refuse upgrade: {bad} {_ATTEMPT} rows have no valid non-negative payload amount"
        )
    op.execute(
        f"ALTER TABLE public.{_ATTEMPT} ADD COLUMN intended_amount numeric "
        "GENERATED ALWAYS AS ((normalized_payload->>'amount')::numeric) STORED"
    )
    op.execute(
        f"ALTER TABLE public.{_ATTEMPT} ADD CONSTRAINT {_AMOUNT_CHECK} "
        "CHECK (intended_amount >= 0 AND intended_amount < 'Infinity'::numeric)"
    )
    op.execute(
        f"CREATE INDEX {_RESOLVED_INDEX} ON public.execution_resolution_journal "
        "(exchange_account_id, deployment_environment, resolved_at_ms DESC, id DESC)"
    )
    if _role_exists(_ROLE):
        for table, columns in WEBAPI_LEDGER_COLUMNS.items():
            op.execute(f"GRANT SELECT ({', '.join(columns)}) ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    if _role_exists(_ROLE):
        for table, columns in WEBAPI_LEDGER_COLUMNS.items():
            op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON public.{table} FROM {_ROLE}")
    op.execute(f"DROP INDEX public.{_RESOLVED_INDEX}")
    op.execute(f"ALTER TABLE public.{_ATTEMPT} DROP CONSTRAINT {_AMOUNT_CHECK}")
    op.execute(f"ALTER TABLE public.{_ATTEMPT} DROP COLUMN intended_amount")
