"""Add the durable Halt 2 one-shot command permit."""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7b1c2d3e4f5"
down_revision: str | Sequence[str] | None = "de6f708192a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "canary_command_permits",
        sa.Column(
            "permit_id",
            _UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("halt_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("cell", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(), nullable=False),
        sa.Column("operator_id", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'issued'"),
        ),
        sa.Column("issued_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("execution_decision_id", sa.Text(), nullable=True),
        sa.Column("attempt_id", _UUID, nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "state IN ('issued', 'consumed')",
            name="ck_canary_permits_state",
        ),
        sa.CheckConstraint(
            "amount_usdt > 0",
            name="ck_canary_permits_amount_positive",
        ),
        sa.CheckConstraint(
            "issued_at_ms >= 0 AND (consumed_at_ms IS NULL OR consumed_at_ms >= issued_at_ms)",
            name="ck_canary_permits_timestamps",
        ),
        sa.CheckConstraint(
            "(state = 'issued' AND consumed_at_ms IS NULL) OR "
            "(state = 'consumed' AND consumed_at_ms IS NOT NULL)",
            name="ck_canary_permits_state_timestamp",
        ),
        sa.ForeignKeyConstraint(
            ["halt_id"], ["trading_halt.id"],
            name="fk_canary_permits_halt", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"],
            name="fk_canary_permits_account", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_decision_id"], ["execution_decisions.decision_id"],
            name="fk_canary_permits_decision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["submission_attempts.attempt_id"],
            name="fk_canary_permits_attempt", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_canary_permits_scope",
        "canary_command_permits",
        ["exchange_account_id", "deployment_environment", "state"],
    )


def downgrade() -> None:
    op.drop_index("idx_canary_permits_scope", table_name="canary_command_permits")
    op.drop_table("canary_command_permits")
