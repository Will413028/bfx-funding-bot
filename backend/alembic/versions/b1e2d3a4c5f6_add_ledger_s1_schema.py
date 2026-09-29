"""Add dormant ledger S1 authority tables and least-privilege roles.

Revision ID: b1e2d3a4c5f6
Revises: 9a4d6e2c7b18
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b1e2d3a4c5f6"
down_revision = "9a4d6e2c7b18"
branch_labels = None
depends_on = None
# New empty tables only; the legacy projector and archive contracts are untouched.
ledger_contract = "preserved"

_TABLE_COLUMNS = {
    "capital_command_clock": ("exchange_account_id", "deployment_environment", "revision"),
    "ledger_observation_query": (
        "query_id",
        "exchange_account_id",
        "deployment_environment",
        "query_revision",
        "started_at_ms",
        "start_revision",
    ),
    "ledger_observation": (
        "id",
        "query_id",
        "exchange_account_id",
        "deployment_environment",
        "schema_version",
        "query_finished_at_ms",
        "confirmation_finished_at_ms",
        "accept_revision",
        "wallets_complete",
        "offers_complete",
        "credits_complete",
        "loans_complete",
        "offer_history_complete",
        "credit_history_complete",
        "offer_history_pages",
        "credit_history_pages",
        "history_requested_start_ms",
        "history_requested_end_ms",
        "history_oldest_mts_created",
        "history_newest_mts_created",
        "first_digest",
        "confirmation_digest",
        "accepted",
        "evidence",
    ),
    "ledger_observation_wallet": (
        "observation_id",
        "wallet_type",
        "currency",
        "available",
        "balance",
    ),
    "ledger_observation_offer": (
        "id",
        "observation_id",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
        "raw",
    ),
    "ledger_observation_credit": (
        "id",
        "observation_id",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
        "raw",
    ),
    "ledger_observation_offer_history": (
        "id",
        "observation_id",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
        "terminal_kind",
        "occurred_at_ms",
        "raw",
    ),
    "ledger_observation_credit_history": (
        "id",
        "observation_id",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
        "terminal_kind",
        "occurred_at_ms",
        "raw",
    ),
    "venue_offer_mirror": (
        "exchange_account_id",
        "deployment_environment",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
        "last_accepted_observation_id",
        "present_in_latest_accepted_snapshot",
        "terminal_evidence_id",
        "terminal_kind",
    ),
    "venue_credit_mirror": (
        "exchange_account_id",
        "deployment_environment",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
        "last_accepted_observation_id",
        "present_in_latest_accepted_snapshot",
        "terminal_evidence_id",
        "terminal_kind",
    ),
    "accepted_capital_basis": (
        "id",
        "exchange_account_id",
        "deployment_environment",
        "observation_id",
        "accepted",
        "accept_revision",
        "policy_revision_id",
        "authorization_block",
        "credit_cells_present",
        "schema_version",
        "digest",
        "accepted_at_ms",
    ),
    "accepted_capital_basis_symbol": (
        "basis_id",
        "symbol",
        "available",
        "offered",
        "credits",
        "unattributed_credits",
        "foreign_offers",
    ),
    "accepted_capital_basis_cell": ("basis_id", "symbol", "cell_id", "amount"),
    "quarantine_opening": (
        "quarantine_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "intended_amount",
        "opened_at_ms",
        "evidence",
        "legacy_reconcile_event_seq",
    ),
    "submission_attempt_journal": (
        "attempt_id",
        "execution_decision_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "attempt_seq",
        "normalized_payload",
        "payload_sha256",
        "basis_id",
        "policy_revision_id",
        "authorization_evidence",
        "seed_provenance",
        "started_at_ms",
    ),
    "transport_outcome_journal": (
        "attempt_id",
        "kind",
        "venue_offer_id",
        "reason",
        "completed_at_ms",
        "evidence",
    ),
    "quarantine_member": ("quarantine_id", "venue_offer_id", "observation_id", "amount_at_join"),
    "execution_resolution_journal": (
        "id",
        "attempt_id",
        "quarantine_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "action",
        "venue_offer_id",
        "observation_id",
        "actor_kind",
        "actor_id",
        "operator_request_id",
        "resolved_at_ms",
        "candidate_count",
        "reason",
        "evidence",
    ),
    "accepted_capital_basis_attempt": ("basis_id", "attempt_id", "symbol", "classification"),
    "accepted_capital_basis_quarantine": ("basis_id", "quarantine_id"),
}
_TABLES = tuple(_TABLE_COLUMNS)
_MIRRORS = {"venue_offer_mirror", "venue_credit_mirror"}
_CLOCK = "capital_command_clock"
_IMMUTABLE = tuple(name for name in _TABLES if name not in _MIRRORS | {_CLOCK})
_FUNCTIONS = (
    "reject_ledger_mutation",
    "guard_ledger_mirror",
    "guard_ledger_scope",
    "guard_ledger_observation_accept",
)

# The cutover reader gets only the columns needed to attest and compare facts.
# Raw venue payloads and authorization evidence stay private.
_READER_COLUMNS = {
    "capital_command_clock": ("exchange_account_id", "deployment_environment", "revision"),
    "ledger_observation_query": (
        "query_id",
        "exchange_account_id",
        "deployment_environment",
        "query_revision",
        "started_at_ms",
        "start_revision",
    ),
    "ledger_observation": (
        "id",
        "query_id",
        "exchange_account_id",
        "deployment_environment",
        "schema_version",
        "query_finished_at_ms",
        "confirmation_finished_at_ms",
        "accept_revision",
        "wallets_complete",
        "offers_complete",
        "credits_complete",
        "loans_complete",
        "offer_history_complete",
        "credit_history_complete",
        "offer_history_pages",
        "credit_history_pages",
        "history_requested_start_ms",
        "history_requested_end_ms",
        "history_oldest_mts_created",
        "history_newest_mts_created",
        "first_digest",
        "confirmation_digest",
        "accepted",
    ),
    "ledger_observation_wallet": (
        "observation_id",
        "wallet_type",
        "currency",
        "available",
        "balance",
    ),
    "ledger_observation_offer": (
        "id",
        "observation_id",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
    ),
    "ledger_observation_credit": (
        "id",
        "observation_id",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
    ),
    "ledger_observation_offer_history": (
        "id",
        "observation_id",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
        "terminal_kind",
        "occurred_at_ms",
    ),
    "ledger_observation_credit_history": (
        "id",
        "observation_id",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
        "terminal_kind",
        "occurred_at_ms",
    ),
    "venue_offer_mirror": (
        "exchange_account_id",
        "deployment_environment",
        "venue_offer_id",
        "symbol",
        "amount_original",
        "amount_remaining",
        "rate",
        "rate_observed",
        "period_days",
        "offer_type",
        "flags",
        "status",
        "mts_created",
        "mts_updated",
        "last_accepted_observation_id",
        "present_in_latest_accepted_snapshot",
        "terminal_evidence_id",
        "terminal_kind",
    ),
    "venue_credit_mirror": (
        "exchange_account_id",
        "deployment_environment",
        "venue_credit_id",
        "source_kind",
        "symbol",
        "amount",
        "rate",
        "period_days",
        "status",
        "flags",
        "mts_created",
        "mts_updated",
        "mts_opening",
        "last_accepted_observation_id",
        "present_in_latest_accepted_snapshot",
        "terminal_evidence_id",
        "terminal_kind",
    ),
    "accepted_capital_basis": (
        "id",
        "exchange_account_id",
        "deployment_environment",
        "observation_id",
        "accepted",
        "accept_revision",
        "policy_revision_id",
        "authorization_block",
        "credit_cells_present",
        "schema_version",
        "digest",
        "accepted_at_ms",
    ),
    "accepted_capital_basis_symbol": (
        "basis_id",
        "symbol",
        "available",
        "offered",
        "credits",
        "unattributed_credits",
        "foreign_offers",
    ),
    "accepted_capital_basis_cell": ("basis_id", "symbol", "cell_id", "amount"),
    "quarantine_opening": (
        "quarantine_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "intended_amount",
        "opened_at_ms",
        "legacy_reconcile_event_seq",
    ),
    "submission_attempt_journal": (
        "attempt_id",
        "execution_decision_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "attempt_seq",
        "payload_sha256",
        "basis_id",
        "policy_revision_id",
        "started_at_ms",
    ),
    "transport_outcome_journal": (
        "attempt_id",
        "kind",
        "venue_offer_id",
        "reason",
        "completed_at_ms",
    ),
    "quarantine_member": ("quarantine_id", "venue_offer_id", "observation_id", "amount_at_join"),
    "execution_resolution_journal": (
        "id",
        "attempt_id",
        "quarantine_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "action",
        "venue_offer_id",
        "observation_id",
        "actor_kind",
        "actor_id",
        "operator_request_id",
        "resolved_at_ms",
        "candidate_count",
        "reason",
    ),
    "accepted_capital_basis_attempt": ("basis_id", "attempt_id", "symbol", "classification"),
    "accepted_capital_basis_quarantine": ("basis_id", "quarantine_id"),
}


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _role_marker() -> str:
    database = op.get_bind().execute(text("SELECT current_database()")).scalar_one()
    return f"created by b1e2d3a4c5f6 in {database}"


def _revoke_defaults(role: str) -> None:
    for name, columns in _TABLE_COLUMNS.items():
        op.execute(f"REVOKE ALL ON TABLE public.{name} FROM {role}")
        op.execute(f"REVOKE ALL ({', '.join(columns)}) ON TABLE public.{name} FROM {role}")


def upgrade() -> None:
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "capital_command_clock",
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_capital_command_clock_revision"),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("exchange_account_id", "deployment_environment"),
    )
    op.create_table(
        "ledger_observation_query",
        sa.Column("query_id", sa.UUID(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("query_revision", sa.BigInteger(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("start_revision", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "query_revision > 0 AND started_at_ms >= 0 AND start_revision >= 0",
            name="ck_ledger_observation_query_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("query_id"),
        sa.UniqueConstraint(
            "exchange_account_id",
            "deployment_environment",
            "query_revision",
            name="uq_ledger_observation_query_scope_revision",
        ),
    )
    op.create_index(
        "ix_ledger_observation_query_scope_revision",
        "ledger_observation_query",
        ["exchange_account_id", "deployment_environment", sa.literal_column("query_revision DESC")],
        unique=False,
    )
    op.create_table(
        "quarantine_opening",
        sa.Column("quarantine_id", sa.UUID(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("intended_amount", sa.Numeric(), nullable=False),
        sa.Column("opened_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("legacy_reconcile_event_seq", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "intended_amount >= 0 AND opened_at_ms >= 0 AND (legacy_reconcile_event_seq IS NULL OR legacy_reconcile_event_seq >= 0)",
            name="ck_quarantine_opening_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("quarantine_id"),
    )
    op.create_table(
        "ledger_observation",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("query_id", sa.UUID(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("query_finished_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("confirmation_finished_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("accept_revision", sa.BigInteger(), nullable=False),
        sa.Column("wallets_complete", sa.Boolean(), nullable=False),
        sa.Column("offers_complete", sa.Boolean(), nullable=False),
        sa.Column("credits_complete", sa.Boolean(), nullable=False),
        sa.Column("loans_complete", sa.Boolean(), nullable=False),
        sa.Column("offer_history_complete", sa.Boolean(), nullable=False),
        sa.Column("credit_history_complete", sa.Boolean(), nullable=False),
        sa.Column("offer_history_pages", sa.Integer(), nullable=True),
        sa.Column("credit_history_pages", sa.Integer(), nullable=True),
        sa.Column("history_requested_start_ms", sa.BigInteger(), nullable=True),
        sa.Column("history_requested_end_ms", sa.BigInteger(), nullable=True),
        sa.Column("history_oldest_mts_created", sa.BigInteger(), nullable=True),
        sa.Column("history_newest_mts_created", sa.BigInteger(), nullable=True),
        sa.Column("first_digest", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(history_requested_start_ms IS NULL) = (history_requested_end_ms IS NULL) AND (history_requested_start_ms IS NULL OR (history_requested_start_ms >= 0 AND history_requested_end_ms >= history_requested_start_ms)) AND (history_oldest_mts_created IS NULL) = (history_newest_mts_created IS NULL) AND (history_oldest_mts_created IS NULL OR (history_oldest_mts_created >= 0 AND history_newest_mts_created >= history_oldest_mts_created))",
            name="ck_ledger_observation_history_range",
        ),
        sa.CheckConstraint(
            "(offer_history_pages IS NULL OR offer_history_pages >= 0) AND (credit_history_pages IS NULL OR credit_history_pages >= 0)",
            name="ck_ledger_observation_history_pages",
        ),
        sa.CheckConstraint(
            "NOT accepted OR (wallets_complete AND offers_complete AND credits_complete AND loans_complete AND offer_history_complete AND credit_history_complete)",
            name="ck_ledger_observation_acceptance",
        ),
        sa.CheckConstraint(
            "first_digest = confirmation_digest", name="ck_ledger_observation_matching_digest"
        ),
        sa.CheckConstraint(
            "schema_version >= 1 AND query_finished_at_ms >= 0 AND confirmation_finished_at_ms >= query_finished_at_ms AND accept_revision >= 0",
            name="ck_ledger_observation_order",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["query_id"], ["ledger_observation_query.query_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "accepted", name="uq_ledger_observation_accepted"),
        sa.UniqueConstraint("query_id"),
    )
    op.create_index(
        "ix_ledger_observation_scope_finished",
        "ledger_observation",
        [
            "exchange_account_id",
            "deployment_environment",
            sa.literal_column("query_finished_at_ms DESC"),
            "id",
        ],
        unique=False,
    )
    op.create_table(
        "accepted_capital_basis",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("accepted", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("accept_revision", sa.BigInteger(), nullable=False),
        sa.Column("policy_revision_id", sa.UUID(), nullable=False),
        sa.Column(
            "authorization_block",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("credit_cells_present", sa.Boolean(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("accepted_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "accepted AND accept_revision >= 0 AND schema_version >= 1 AND accepted_at_ms >= 0",
            name="ck_accepted_basis_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "accepted"],
            ["ledger_observation.id", "ledger_observation.accepted"],
            name="fk_accepted_basis_observation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision_id"], ["capital_policy_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id"),
    )
    op.create_index(
        "ix_accepted_capital_basis_scope_accepted",
        "accepted_capital_basis",
        ["exchange_account_id", "deployment_environment", sa.literal_column("accepted_at_ms DESC")],
        unique=False,
    )
    op.create_table(
        "ledger_observation_credit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("venue_credit_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("mts_created", sa.BigInteger(), nullable=True),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("mts_opening", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_ledger_observation_credit_source"
        ),
        sa.CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND (mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR mts_opening >= 0)",
            name="ck_ledger_observation_credit_amount",
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id",
            "venue_credit_id",
            "source_kind",
            name="uq_ledger_observation_credit_venue",
        ),
    )
    op.create_table(
        "ledger_observation_credit_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("venue_credit_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("mts_created", sa.BigInteger(), nullable=True),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("mts_opening", sa.BigInteger(), nullable=True),
        sa.Column("terminal_kind", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_ledger_credit_history_source"
        ),
        sa.CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND (mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR mts_opening >= 0) AND occurred_at_ms >= 0",
            name="ck_ledger_credit_history_time",
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ledger_observation_credit_history_observation",
        "ledger_observation_credit_history",
        ["observation_id"],
        unique=False,
    )
    op.create_table(
        "ledger_observation_offer",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount_original", sa.Numeric(), nullable=True),
        sa.Column("amount_remaining", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("rate_observed", sa.Boolean(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("offer_type", sa.Text(), nullable=True),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mts_created", sa.BigInteger(), nullable=False),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_ledger_observation_offer_amount",
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id", "venue_offer_id", name="uq_ledger_observation_offer_venue"
        ),
    )
    op.create_table(
        "ledger_observation_offer_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount_original", sa.Numeric(), nullable=True),
        sa.Column("amount_remaining", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("rate_observed", sa.Boolean(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("offer_type", sa.Text(), nullable=True),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mts_created", sa.BigInteger(), nullable=False),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("terminal_kind", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0) AND occurred_at_ms >= 0",
            name="ck_ledger_offer_history_time",
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ledger_observation_offer_history_observation",
        "ledger_observation_offer_history",
        ["observation_id"],
        unique=False,
    )
    op.create_table(
        "ledger_observation_wallet",
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("wallet_type", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("available", sa.Numeric(), nullable=False),
        sa.Column("balance", sa.Numeric(), nullable=False),
        sa.CheckConstraint("available >= 0 AND balance >= 0", name="ck_ledger_wallet_amount"),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("observation_id", "wallet_type", "currency"),
    )
    op.create_table(
        "quarantine_member",
        sa.Column("quarantine_id", sa.UUID(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("amount_at_join", sa.Numeric(), nullable=False),
        sa.CheckConstraint("amount_at_join >= 0", name="ck_quarantine_member_amount"),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["quarantine_id"], ["quarantine_opening.quarantine_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("quarantine_id", "venue_offer_id"),
    )
    op.create_table(
        "accepted_capital_basis_cell",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("cell_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_accepted_basis_cell_amount"),
        sa.ForeignKeyConstraint(["basis_id"], ["accepted_capital_basis.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("basis_id", "symbol", "cell_id"),
    )
    op.create_table(
        "accepted_capital_basis_quarantine",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("quarantine_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["basis_id"], ["accepted_capital_basis.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["quarantine_id"], ["quarantine_opening.quarantine_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("basis_id", "quarantine_id"),
    )
    op.create_table(
        "accepted_capital_basis_symbol",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("available", sa.Numeric(), nullable=False),
        sa.Column("offered", sa.Numeric(), nullable=False),
        sa.Column("credits", sa.Numeric(), nullable=False),
        sa.Column("unattributed_credits", sa.Numeric(), nullable=False),
        sa.Column("foreign_offers", sa.Numeric(), nullable=False),
        sa.CheckConstraint(
            "available >= 0 AND offered >= 0 AND credits >= 0 AND unattributed_credits >= 0 AND foreign_offers >= 0 AND unattributed_credits <= credits",
            name="ck_accepted_basis_symbol_amount",
        ),
        sa.ForeignKeyConstraint(["basis_id"], ["accepted_capital_basis.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("basis_id", "symbol"),
    )
    op.create_table(
        "submission_attempt_journal",
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("execution_decision_id", sa.Text(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("attempt_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "normalized_payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("policy_revision_id", sa.UUID(), nullable=False),
        sa.Column(
            "authorization_evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "seed_provenance",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "attempt_seq >= 0 AND started_at_ms >= 0", name="ck_submission_attempt_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["basis_id"],
            ["accepted_capital_basis.id"],
            name="fk_submission_attempt_basis",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["execution_decision_id"], ["execution_decisions.decision_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision_id"], ["capital_policy_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "exchange_account_id",
            "deployment_environment",
            "attempt_seq",
            name="uq_submission_attempt_scope_seq",
        ),
        sa.UniqueConstraint("execution_decision_id"),
    )
    op.create_table(
        "venue_credit_mirror",
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("venue_credit_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("mts_created", sa.BigInteger(), nullable=True),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("mts_opening", sa.BigInteger(), nullable=True),
        sa.Column("last_accepted_observation_id", sa.UUID(), nullable=False),
        sa.Column("present_in_latest_accepted_snapshot", sa.Boolean(), nullable=False),
        sa.Column("terminal_evidence_id", sa.UUID(), nullable=True),
        sa.Column("terminal_kind", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_venue_credit_mirror_source"
        ),
        sa.CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND (terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_credit_mirror_terminal",
        ),
        sa.CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND (mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR mts_opening >= 0)",
            name="ck_venue_credit_mirror_amount",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_accepted_observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["terminal_evidence_id"], ["ledger_observation_credit_history.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "venue_credit_id", "source_kind"
        ),
    )
    op.create_index(
        "ix_venue_credit_mirror_live",
        "venue_credit_mirror",
        ["exchange_account_id", "deployment_environment", "symbol"],
        unique=False,
        postgresql_where=sa.text("present_in_latest_accepted_snapshot"),
    )
    op.create_table(
        "venue_offer_mirror",
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount_original", sa.Numeric(), nullable=True),
        sa.Column("amount_remaining", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("rate_observed", sa.Boolean(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("offer_type", sa.Text(), nullable=True),
        sa.Column(
            "flags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mts_created", sa.BigInteger(), nullable=False),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("last_accepted_observation_id", sa.UUID(), nullable=False),
        sa.Column("present_in_latest_accepted_snapshot", sa.Boolean(), nullable=False),
        sa.Column("terminal_evidence_id", sa.UUID(), nullable=True),
        sa.Column("terminal_kind", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND (terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_offer_mirror_terminal",
        ),
        sa.CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_venue_offer_mirror_amount",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_accepted_observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["terminal_evidence_id"], ["ledger_observation_offer_history.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "venue_offer_id"),
    )
    op.create_index(
        "ix_venue_offer_mirror_live",
        "venue_offer_mirror",
        ["exchange_account_id", "deployment_environment", "symbol"],
        unique=False,
        postgresql_where=sa.text("present_in_latest_accepted_snapshot"),
    )
    op.create_table(
        "accepted_capital_basis_attempt",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "classification IN ('reflected','settled','unresolved')",
            name="ck_accepted_basis_attempt_classification",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["submission_attempt_journal.attempt_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["basis_id"], ["accepted_capital_basis.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("basis_id", "attempt_id"),
    )
    op.create_table(
        "execution_resolution_journal",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("quarantine_id", sa.UUID(), nullable=True),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("operator_request_id", sa.UUID(), nullable=True),
        sa.Column("resolved_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(action = 'bound_to_venue') = (venue_offer_id IS NOT NULL)",
            name="ck_execution_resolution_bound",
        ),
        sa.CheckConstraint(
            "action IN ('bound_to_venue','not_accepted','manual')",
            name="ck_execution_resolution_action",
        ),
        sa.CheckConstraint(
            "(attempt_id IS NULL) <> (quarantine_id IS NULL)",
            name="ck_execution_resolution_subject",
        ),
        sa.CheckConstraint(
            "resolved_at_ms >= 0 AND (candidate_count IS NULL OR candidate_count >= 0)",
            name="ck_execution_resolution_time",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["submission_attempt_journal.attempt_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["operator_request_id"],
            ["uncertainty_resolution_requests.request_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quarantine_id"], ["quarantine_opening.quarantine_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_execution_resolution_attempt",
        "execution_resolution_journal",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )
    op.create_index(
        "uq_execution_resolution_quarantine",
        "execution_resolution_journal",
        ["quarantine_id"],
        unique=True,
        postgresql_where=sa.text("quarantine_id IS NOT NULL"),
    )
    op.create_table(
        "transport_outcome_journal",
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'ack') = (venue_offer_id IS NOT NULL)", name="ck_transport_outcome_ack"
        ),
        sa.CheckConstraint(
            "kind IN ('ack','rejected','not_sent','unknown')", name="ck_transport_outcome_kind"
        ),
        sa.CheckConstraint("completed_at_ms >= 0", name="ck_transport_outcome_time"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["submission_attempt_journal.attempt_id"],
            name="fk_basis_attempt_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute("""CREATE FUNCTION public.reject_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable ledger evidence'; END $$""")
    op.execute("""CREATE FUNCTION public.guard_ledger_mirror() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
            RAISE EXCEPTION 'immutable ledger mirror deletion';
          END IF;
          IF OLD.terminal_evidence_id IS NOT NULL
             AND (NEW.terminal_evidence_id IS DISTINCT FROM OLD.terminal_evidence_id
               OR NEW.terminal_kind IS DISTINCT FROM OLD.terminal_kind
               OR NEW.present_in_latest_accepted_snapshot) THEN
            RAISE EXCEPTION 'immutable ledger terminal evidence';
          END IF;
          IF NEW.exchange_account_id IS DISTINCT FROM OLD.exchange_account_id
             OR NEW.deployment_environment IS DISTINCT FROM OLD.deployment_environment THEN
            RAISE EXCEPTION 'immutable ledger mirror scope';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE FUNCTION public.guard_ledger_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE parent_scope record;
        BEGIN
          IF TG_TABLE_NAME = 'submission_attempt_journal' THEN
            SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
              FROM public.execution_decisions WHERE decision_id = NEW.execution_decision_id;
            IF parent_scope.exchange_account_id IS NULL
               OR (parent_scope.exchange_account_id,
                   parent_scope.deployment_environment, parent_scope.symbol)
                  IS DISTINCT FROM (NEW.exchange_account_id,
                    NEW.deployment_environment, NEW.symbol) THEN
              RAISE EXCEPTION 'ledger decision scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'quarantine_member' THEN
            SELECT q.exchange_account_id, q.deployment_environment, q.symbol INTO parent_scope
              FROM public.quarantine_opening q WHERE q.quarantine_id = NEW.quarantine_id;
            IF NOT EXISTS (
              SELECT 1 FROM public.ledger_observation o
              JOIN (
                SELECT observation_id, venue_offer_id, symbol FROM public.ledger_observation_offer
                UNION ALL
                SELECT observation_id, venue_offer_id, symbol
                  FROM public.ledger_observation_offer_history
              ) h ON h.observation_id = o.id
              WHERE o.id = NEW.observation_id AND h.venue_offer_id = NEW.venue_offer_id
                AND (o.exchange_account_id, o.deployment_environment, h.symbol)
                    = (parent_scope.exchange_account_id,
                       parent_scope.deployment_environment, parent_scope.symbol)) THEN
              RAISE EXCEPTION 'ledger member scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_resolution_journal' THEN
            IF NEW.attempt_id IS NOT NULL THEN
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.submission_attempt_journal WHERE attempt_id = NEW.attempt_id;
            ELSE
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.quarantine_opening WHERE quarantine_id = NEW.quarantine_id;
            END IF;
            IF (parent_scope.exchange_account_id,
                parent_scope.deployment_environment, parent_scope.symbol)
                 IS DISTINCT FROM (NEW.exchange_account_id, NEW.deployment_environment, NEW.symbol)
               OR NOT EXISTS (SELECT 1 FROM public.ledger_observation o
                 WHERE o.id = NEW.observation_id
                 AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment)) THEN
              RAISE EXCEPTION 'ledger resolution scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'accepted_capital_basis' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o WHERE o.id = NEW.observation_id
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment)
              AND o.accept_revision = NEW.accept_revision) THEN
              RAISE EXCEPTION 'ledger basis scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_offer_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_offer_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_offer_id = NEW.venue_offer_id
                  AND h.symbol = NEW.symbol AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger offer mirror scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_credit_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_credit_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_credit_id = NEW.venue_credit_id
                  AND h.source_kind = NEW.source_kind AND h.symbol = NEW.symbol
                  AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger credit mirror scope mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE FUNCTION public.guard_ledger_observation_accept() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE query_row record;
        BEGIN
          SELECT exchange_account_id, deployment_environment, query_revision,
                 started_at_ms, start_revision INTO query_row
            FROM public.ledger_observation_query WHERE query_id = NEW.query_id;
          IF NOT FOUND OR
             (query_row.exchange_account_id, query_row.deployment_environment)
               IS DISTINCT FROM (NEW.exchange_account_id, NEW.deployment_environment)
             OR NEW.query_finished_at_ms < query_row.started_at_ms THEN
            RAISE EXCEPTION 'ledger observation query scope mismatch';
          END IF;
          IF NEW.accepted AND (
             NEW.accept_revision IS DISTINCT FROM query_row.start_revision
             OR NOT EXISTS (
               SELECT 1 FROM public.capital_command_clock c
               WHERE (c.exchange_account_id, c.deployment_environment, c.revision)
                 = (NEW.exchange_account_id, NEW.deployment_environment, NEW.accept_revision))
             OR query_row.query_revision IS DISTINCT FROM (
               SELECT max(q.query_revision) FROM public.ledger_observation_query q
               WHERE (q.exchange_account_id, q.deployment_environment)
                 = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
            RAISE EXCEPTION 'ledger observation accept fence';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER guard_ledger_observation_accept_insert BEFORE INSERT "
        "ON public.ledger_observation FOR EACH ROW "
        "EXECUTE FUNCTION public.guard_ledger_observation_accept()"
    )
    for name in _IMMUTABLE:
        op.execute(
            f"CREATE TRIGGER immutable_ledger_write BEFORE UPDATE OR DELETE ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_ledger_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER immutable_ledger_truncate BEFORE TRUNCATE ON public.{name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_ledger_mutation()"
        )
    for name in _MIRRORS:
        op.execute(
            f"CREATE TRIGGER guard_ledger_mirror_write BEFORE UPDATE OR DELETE ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_ledger_mirror()"
        )
        op.execute(
            f"CREATE TRIGGER guard_ledger_mirror_truncate BEFORE TRUNCATE ON public.{name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.guard_ledger_mirror()"
        )
    for name in (
        "submission_attempt_journal",
        "quarantine_member",
        "execution_resolution_journal",
        "accepted_capital_basis",
    ):
        op.execute(
            f"CREATE TRIGGER guard_ledger_scope_insert BEFORE INSERT ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_ledger_scope()"
        )
    for name in _MIRRORS:
        op.execute(
            f"CREATE TRIGGER guard_ledger_mirror_scope BEFORE INSERT OR UPDATE ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_ledger_scope()"
        )
    for function in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")

    if not _role_exists("bfx_cutover_reader"):
        op.execute("CREATE ROLE bfx_cutover_reader NOLOGIN")
        marker = _role_marker().replace("'", "''")
        op.execute(f"COMMENT ON ROLE bfx_cutover_reader IS '{marker}'")
    else:
        if (
            op.get_bind()
            .execute(text("SELECT rolcanlogin FROM pg_roles WHERE rolname='bfx_cutover_reader'"))
            .scalar()
        ):
            raise RuntimeError("bfx_cutover_reader must be NOLOGIN")
    # Even a pre-existing group may have broad grants from owner defaults.
    for name, columns in _TABLE_COLUMNS.items():
        op.execute(f"REVOKE ALL ON TABLE public.{name} FROM PUBLIC")
        op.execute(f"REVOKE ALL ({', '.join(columns)}) ON TABLE public.{name} FROM PUBLIC")
    for role in ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
        if _role_exists(role):
            _revoke_defaults(role)
    op.execute("GRANT USAGE ON SCHEMA public TO bfx_cutover_reader")
    for name, columns in _READER_COLUMNS.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON public.{name} TO bfx_cutover_reader")
    if _role_exists("bfx_bot"):
        for name in _IMMUTABLE:
            op.execute(f"GRANT SELECT, INSERT ON public.{name} TO bfx_bot")
        op.execute("GRANT SELECT, INSERT ON public.capital_command_clock TO bfx_bot")
        op.execute("GRANT UPDATE (revision) ON public.capital_command_clock TO bfx_bot")
        for name in _MIRRORS:
            op.execute(f"GRANT SELECT, INSERT, UPDATE ON public.{name} TO bfx_bot")


def downgrade() -> None:
    for name in _TABLES:
        if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{name})")).scalar():
            raise RuntimeError("refuse downgrade of populated ledger: " + name)
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.drop_table("transport_outcome_journal")
    op.drop_index(
        "uq_execution_resolution_quarantine",
        table_name="execution_resolution_journal",
        postgresql_where=sa.text("quarantine_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_execution_resolution_attempt",
        table_name="execution_resolution_journal",
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )
    op.drop_table("execution_resolution_journal")
    op.drop_table("accepted_capital_basis_attempt")
    op.drop_index(
        "ix_venue_offer_mirror_live",
        table_name="venue_offer_mirror",
        postgresql_where=sa.text("present_in_latest_accepted_snapshot"),
    )
    op.drop_table("venue_offer_mirror")
    op.drop_index(
        "ix_venue_credit_mirror_live",
        table_name="venue_credit_mirror",
        postgresql_where=sa.text("present_in_latest_accepted_snapshot"),
    )
    op.drop_table("venue_credit_mirror")
    op.drop_table("submission_attempt_journal")
    op.drop_table("accepted_capital_basis_symbol")
    op.drop_table("accepted_capital_basis_quarantine")
    op.drop_table("accepted_capital_basis_cell")
    op.drop_table("quarantine_member")
    op.drop_table("ledger_observation_wallet")
    op.drop_index(
        "ix_ledger_observation_offer_history_observation",
        table_name="ledger_observation_offer_history",
    )
    op.drop_table("ledger_observation_offer_history")
    op.drop_table("ledger_observation_offer")
    op.drop_index(
        "ix_ledger_observation_credit_history_observation",
        table_name="ledger_observation_credit_history",
    )
    op.drop_table("ledger_observation_credit_history")
    op.drop_table("ledger_observation_credit")
    op.drop_index("ix_accepted_capital_basis_scope_accepted", table_name="accepted_capital_basis")
    op.drop_table("accepted_capital_basis")
    op.drop_index("ix_ledger_observation_scope_finished", table_name="ledger_observation")
    op.drop_table("ledger_observation")
    op.drop_table("quarantine_opening")
    op.drop_index(
        "ix_ledger_observation_query_scope_revision", table_name="ledger_observation_query"
    )
    op.drop_table("ledger_observation_query")
    op.drop_table("capital_command_clock")
    # END AUTOGEN-DDL-DOWNGRADE
    # in reverse FK-dependency order.
    for function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{function}()")
    if (
        _role_exists("bfx_cutover_reader")
        and op.get_bind()
        .execute(
            text(
                "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles "
                "WHERE rolname='bfx_cutover_reader'"
            )
        )
        .scalar()
        == _role_marker()
    ):
        op.execute("REVOKE USAGE ON SCHEMA public FROM bfx_cutover_reader")
        op.execute("DROP ROLE bfx_cutover_reader")
