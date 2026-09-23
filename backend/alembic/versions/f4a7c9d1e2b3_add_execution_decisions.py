"""add execution_decisions

Revision ID: f4a7c9d1e2b3
Revises: d437f9d4e3fe
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "f4a7c9d1e2b3"
down_revision: str | Sequence[str] | None = "d437f9d4e3fe"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "execution_decisions",
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("reconcile_id", sa.Text(), nullable=False),
        sa.Column("cell_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("signal_correlation_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("failed_dependency", sa.Text(), nullable=True),
        sa.Column("signal_rate", sa.Numeric(), nullable=False),
        sa.Column("applied_rate", sa.Numeric(), nullable=True),
        sa.Column("amount_usdt", sa.Numeric(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=True),
        sa.Column("snapshot_captured_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_source", sa.Text(), nullable=True),
        sa.Column("snapshot_age_ms", sa.BigInteger(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("model_hash", sa.Text(), nullable=True),
        sa.Column("model_evidence", _JSON, nullable=False),
        sa.Column("safety_result", _JSON, nullable=False),
        sa.Column("execution_policy", sa.Text(), nullable=False),
        sa.Column("service_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index(
        "idx_execution_decisions_env_occurred",
        "execution_decisions",
        ["deployment_environment", "occurred_at_ms"],
    )
    op.create_index(
        "idx_execution_decisions_symbol_occurred",
        "execution_decisions",
        ["symbol", "occurred_at_ms"],
    )
    op.create_index(
        "idx_execution_decisions_outcome_reason",
        "execution_decisions",
        ["outcome", "reason_code"],
    )


def downgrade() -> None:
    op.drop_index("idx_execution_decisions_outcome_reason", table_name="execution_decisions")
    op.drop_index("idx_execution_decisions_symbol_occurred", table_name="execution_decisions")
    op.drop_index("idx_execution_decisions_env_occurred", table_name="execution_decisions")
    op.drop_table("execution_decisions")
