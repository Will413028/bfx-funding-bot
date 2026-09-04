"""Add schema-v3 event identity and account-scoped projector state.

This is the additive half of the serialized execution cutover.  Event history
is immutable: historical rows keep a NULL ``event_id`` and the application
upcaster derives their UUIDv5 identity during replay.  The legacy position
columns remain mapped until the full-account observation cutover has proven the
new buckets; that later contract migration can then remove them without
mixing a data rewrite with this DDL boundary.  No new writer may treat the
legacy columns as an independent source of truth after the Task 3/4 cutover.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "bc4d5e6f7081"
down_revision: str | Sequence[str] | None = "9b2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Install the identity, cursor, and normalized venue-object tables."""
    # Event identity is nullable by design for historical rows.  The check
    # protects every new schema-v3 write while allowing the append-only history
    # to be replayed without an in-place payload/row rewrite.
    op.add_column(
        "event_log",
        sa.Column("event_id", _UUID, nullable=True),
    )
    op.add_column(
        "event_log",
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("2"),
        ),
    )
    op.create_check_constraint(
        "ck_event_log_v3_event_id",
        "event_log",
        "schema_version < 3 OR event_id IS NOT NULL",
    )
    op.create_index(
        "uq_event_log_event_id",
        "event_log",
        ["exchange_account_id", "deployment_environment", "event_id"],
        unique=True,
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )
    op.create_index(
        "uq_event_log_legacy_event_id",
        "event_log",
        ["account_id", "deployment_environment", "event_id"],
        unique=True,
        postgresql_where=sa.text(
            "event_id IS NOT NULL AND exchange_account_id IS NULL"
        ),
    )

    # Expand position_state first.  Existing reserved/realized values are the
    # only durable delta projection available before observation events exist;
    # copy them into the canonical offer/lent buckets and keep the old columns
    # as read-compatible provenance until the next clean cutover.
    for name in (
        "offered_amount",
        "lent_amount",
        "available_amount",
        "uncertain_amount",
    ):
        op.add_column(
            "position_state",
            sa.Column(
                name,
                sa.Numeric(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    op.add_column(
        "position_state",
        sa.Column("last_venue_snapshot_at", sa.BigInteger(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE position_state "
            "SET offered_amount = COALESCE(reserved, 0), "
            "    lent_amount = COALESCE(realized, 0), "
            "    available_amount = 0, "
            "    uncertain_amount = 0, "
            "    last_venue_snapshot_at = last_reconciled_at"
        )
    )

    op.create_table(
        "projection_heads",
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("projection_name", sa.Text(), nullable=False),
        sa.Column(
            "last_event_seq",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("projector_version", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_projection_heads_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "projection_name"
        ),
    )

    op.create_table(
        "venue_offer_state",
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount_original", sa.Numeric(), nullable=False),
        sa.Column("amount_remaining", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("period", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "flags", _JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("mts_created", sa.BigInteger(), nullable=False),
        sa.Column("mts_updated", sa.BigInteger(), nullable=False),
        sa.Column("cid", sa.BigInteger(), nullable=True),
        sa.Column("execution_decision_id", sa.Text(), nullable=True),
        sa.Column("signal_correlation_id", sa.Text(), nullable=True),
        sa.Column("first_seen_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_event_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_terminal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_venue_offer_state_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "venue_offer_id"
        ),
    )
    op.create_index(
        "idx_venue_offer_state_account_status",
        "venue_offer_state",
        ["exchange_account_id", "deployment_environment", "status"],
    )

    op.create_table(
        "venue_credit_state",
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("credit_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=True),
        sa.Column("period", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "flags", _JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("mts_created", sa.BigInteger(), nullable=True),
        sa.Column("mts_updated", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_event_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_terminal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_venue_credit_state_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "credit_id"
        ),
    )
    op.create_index(
        "idx_venue_credit_state_account_status",
        "venue_credit_state",
        ["exchange_account_id", "deployment_environment", "status"],
    )


def downgrade() -> None:
    """Reverse additive projector DDL in development environments."""
    op.drop_index("idx_venue_credit_state_account_status", table_name="venue_credit_state")
    op.drop_table("venue_credit_state")
    op.drop_index("idx_venue_offer_state_account_status", table_name="venue_offer_state")
    op.drop_table("venue_offer_state")
    op.drop_table("projection_heads")
    op.drop_column("position_state", "last_venue_snapshot_at")
    for name in (
        "uncertain_amount",
        "available_amount",
        "lent_amount",
        "offered_amount",
    ):
        op.drop_column("position_state", name)
    op.drop_index("uq_event_log_legacy_event_id", table_name="event_log")
    op.drop_index("uq_event_log_event_id", table_name="event_log")
    op.drop_constraint("ck_event_log_v3_event_id", "event_log", type_="check")
    op.drop_column("event_log", "schema_version")
    op.drop_column("event_log", "event_id")
