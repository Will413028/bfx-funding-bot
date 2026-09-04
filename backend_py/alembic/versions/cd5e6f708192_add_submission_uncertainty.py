"""Persist immutable submission attempts and scoped execution uncertainties.

The tables are additive projections.  They deliberately preserve an UNKNOWN
attempt and its pessimistic reserve until a later reconcile-linked resolution;
this migration introduces no submit retry, command gate, or operator API.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "cd5e6f708192"
# c2e3f4a5b6c7 is the actual linear head after the serialized-projector cursor
# backfill.  The written Task 2 brief predates that already-merged revision.
down_revision: str | Sequence[str] | None = "c2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Create durable audit identity and pessimistic uncertainty projections."""
    op.create_table(
        "submission_attempts",
        sa.Column(
            "attempt_id",
            _UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("execution_decision_id", sa.Text(), nullable=False, unique=True),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("cid", sa.BigInteger(), nullable=False),
        sa.Column("normalized_payload", _JSONB, nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_kind", sa.Text(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("last_event_seq", sa.BigInteger(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("cid >= 0", name="ck_submission_attempts_cid_nonnegative"),
        sa.CheckConstraint(
            "started_at_ms >= 0 AND (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms)",
            name="ck_submission_attempts_timestamps",
        ),
        sa.CheckConstraint(
            "last_event_seq IS NULL OR last_event_seq >= 0",
            name="ck_submission_attempts_event_seq_nonnegative",
        ),
        sa.CheckConstraint(
            "outcome_kind IS NULL OR outcome_kind IN "
            "('acknowledged', 'rejected', 'unknown', 'not_sent')",
            name="ck_submission_attempts_outcome_kind",
        ),
        sa.ForeignKeyConstraint(
            ["execution_decision_id"],
            ["execution_decisions.decision_id"],
            name="fk_submission_attempts_execution_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_submission_attempts_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_event_seq"],
            ["event_log.event_seq"],
            name="fk_submission_attempts_last_event",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_submission_attempts_scope",
        "submission_attempts",
        ["exchange_account_id", "deployment_environment", "symbol"],
    )

    op.create_table(
        "execution_uncertainties",
        sa.Column(
            "uncertainty_id",
            _UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("correlation_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("intended_amount", sa.Numeric(), nullable=False),
        sa.Column("evidence", _JSONB, nullable=False),
        sa.Column("attempt_id", _UUID, nullable=True),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("opened_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("reconcile_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("resolved_event_seq", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by_operator_id", sa.Text(), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolution_evidence", _JSONB, nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('submit_outcome_unknown', 'unattributed_venue_offer', "
            "'unsupported_venue_exposure')",
            name="ck_execution_uncertainties_kind",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'resolved')",
            name="ck_execution_uncertainties_state",
        ),
        sa.CheckConstraint(
            "intended_amount >= 0",
            name="ck_execution_uncertainties_intended_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "(state = 'open' AND reconcile_event_seq IS NULL AND resolved_event_seq IS NULL "
            "AND resolved_by_operator_id IS NULL AND resolution_reason IS NULL "
            "AND resolution_evidence IS NULL AND resolved_at IS NULL) OR "
            "(state = 'resolved' AND reconcile_event_seq IS NOT NULL AND resolved_event_seq IS NOT NULL "
            "AND resolved_by_operator_id IS NOT NULL AND resolution_reason IS NOT NULL "
            "AND resolution_evidence IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_execution_uncertainties_resolution_shape",
        ),
        sa.CheckConstraint(
            "state = 'open' OR (opened_event_seq < reconcile_event_seq "
            "AND reconcile_event_seq < resolved_event_seq)",
            name="ck_execution_uncertainties_resolution_event_order",
        ),
        sa.CheckConstraint(
            "(kind = 'unattributed_venue_offer' AND venue_offer_id IS NOT NULL) OR "
            "(kind IN ('submit_outcome_unknown', 'unsupported_venue_exposure') "
            "AND venue_offer_id IS NULL)",
            name="ck_execution_uncertainties_venue_link_kind",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_execution_uncertainties_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["submission_attempts.attempt_id"],
            name="fk_execution_uncertainties_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opened_event_seq"],
            ["event_log.event_seq"],
            name="fk_execution_uncertainties_opened_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reconcile_event_seq"],
            ["event_log.event_seq"],
            name="fk_execution_uncertainties_reconcile_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_event_seq"],
            ["event_log.event_seq"],
            name="fk_execution_uncertainties_resolved_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id", "deployment_environment", "venue_offer_id"],
            [
                "venue_offer_state.exchange_account_id",
                "venue_offer_state.deployment_environment",
                "venue_offer_state.venue_offer_id",
            ],
            name="fk_execution_uncertainties_venue_offer",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_execution_uncertainties_correlation",
        "execution_uncertainties",
        [
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            "kind",
            "correlation_key",
        ],
        unique=True,
    )
    op.create_index(
        "uq_execution_uncertainties_open_scope",
        "execution_uncertainties",
        ["exchange_account_id", "deployment_environment", "symbol", "kind"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )


def downgrade() -> None:
    """Remove only the additive Task 2 projections."""
    op.drop_index(
        "uq_execution_uncertainties_open_scope", table_name="execution_uncertainties"
    )
    op.drop_index(
        "uq_execution_uncertainties_correlation", table_name="execution_uncertainties"
    )
    op.drop_table("execution_uncertainties")
    op.drop_index("idx_submission_attempts_scope", table_name="submission_attempts")
    op.drop_table("submission_attempts")
