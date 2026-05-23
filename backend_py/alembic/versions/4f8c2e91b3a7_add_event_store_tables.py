"""add event store tables

Revision ID: 4f8c2e91b3a7
Revises: a376b830a8f1
Create Date: 2026-05-23 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4f8c2e91b3a7"
down_revision: str | Sequence[str] | None = "a376b830a8f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column("event_seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("cid", sa.BigInteger(), nullable=True),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("venue_seq", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_seq"),
    )
    op.create_index(
        "idx_event_log_acct_env_seq",
        "event_log",
        ["account_id", "deployment_environment", "event_seq"],
    )
    op.create_index("idx_event_log_cid", "event_log", ["cid"])
    op.create_index("idx_event_log_voi", "event_log", ["venue_offer_id"])
    op.create_index(
        "uq_event_log_dedup",
        "event_log",
        ["account_id", "deployment_environment", "event_type", "venue_offer_id", "venue_seq"],
        unique=True,
    )
    op.create_table(
        "offer_claims",
        sa.Column("cid", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("size_usdt", sa.Numeric(), nullable=False),
        sa.Column("signal_correlation_id", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_updated_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_event_seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("cid"),
    )
    op.create_index(
        "idx_offer_claims_acct_env",
        "offer_claims",
        ["account_id", "deployment_environment"],
    )
    op.create_index("idx_offer_claims_voi", "offer_claims", ["venue_offer_id"])
    op.create_table(
        "position_state",
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("reserved_usdt", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("realized_usdt", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_event_seq",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("account_id", "deployment_environment"),
    )


def downgrade() -> None:
    op.drop_table("position_state")
    op.drop_index("idx_offer_claims_voi", table_name="offer_claims")
    op.drop_index("idx_offer_claims_acct_env", table_name="offer_claims")
    op.drop_table("offer_claims")
    op.drop_index("uq_event_log_dedup", table_name="event_log")
    op.drop_index("idx_event_log_voi", table_name="event_log")
    op.drop_index("idx_event_log_cid", table_name="event_log")
    op.drop_index("idx_event_log_acct_env_seq", table_name="event_log")
    op.drop_table("event_log")
