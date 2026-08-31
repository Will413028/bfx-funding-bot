"""Add the additive ExchangeAccount identity schema for Halt 1.

This revision deliberately leaves all legacy realm columns and nullable
``exchange_account_id`` backfill columns in place.  Application data
conversion and credential re-encryption are performed by the idempotent
``cutover_identity.py`` command; the contract revision later makes the UUID
columns mandatory after its preflight has passed.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8a1b2c3d4e5f"
down_revision: str | Sequence[str] | None = "f5b8d0e2f3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)

_MONEY_TABLES = (
    "event_log",
    "offer_claims",
    "position_state",
    "reconcile_observation",
    "execution_decisions",
    "diagnostics",
    "nav_peak",
    "trading_halt",
    "attribution_weekly",
    "config_regime",
    "api_keys",
    "user_configs",
)
_ENV_MONEY_TABLES = (
    "event_log",
    "offer_claims",
    "position_state",
    "reconcile_observation",
    "execution_decisions",
    "diagnostics",
    "nav_peak",
    "trading_halt",
    "attribution_weekly",
    "config_regime",
)
_IDENTITY_TABLES = (
    "legacy_account_realm_map",
    "account_config_drafts",
    "exchange_account_credentials",
    "exchange_account_memberships",
    "exchange_accounts",
)


def upgrade() -> None:
    op.create_table(
        "exchange_accounts",
        sa.Column("id", _UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "lifecycle_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'halted', 'retired')",
            name="ck_exchange_accounts_lifecycle_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_exchange_accounts_venue_status",
        "exchange_accounts",
        ["venue", "lifecycle_status"],
    )

    op.create_table(
        "exchange_account_memberships",
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'operator', 'viewer')",
            name="ck_exchange_account_memberships_role",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_exchange_account_memberships_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("exchange_account_id", "user_id"),
    )
    op.create_index(
        "idx_exchange_account_memberships_user",
        "exchange_account_memberships",
        ["user_id"],
    )

    op.create_table(
        "exchange_account_credentials",
        sa.Column("id", _UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("dek_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "lifecycle_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'revoked', 'retired')",
            name="ck_exchange_account_credentials_lifecycle_status",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_exchange_account_credentials_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_exchange_account_credentials_active_venue",
        "exchange_account_credentials",
        ["exchange_account_id", "venue"],
        unique=True,
        postgresql_where=sa.text("lifecycle_status = 'active'"),
    )
    op.create_index(
        "idx_exchange_account_credentials_account",
        "exchange_account_credentials",
        ["exchange_account_id", "created_at"],
    )

    op.create_table(
        "account_config_drafts",
        sa.Column("id", _UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_account_config_drafts_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_account_config_drafts_account",
        "account_config_drafts",
        ["exchange_account_id"],
        unique=True,
    )

    op.create_table(
        "legacy_account_realm_map",
        sa.Column("realm_key", sa.Text(), nullable=False),
        sa.Column("exchange_account_id", _UUID, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_legacy_account_realm_map_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("realm_key"),
    )

    # Expand phase: nullable UUID columns preserve every legacy row until the
    # application cutover has validated the mapping manifest.
    for table_name in _MONEY_TABLES:
        op.add_column(
            table_name,
            sa.Column("exchange_account_id", _UUID, nullable=True),
        )

    for table_name in _ENV_MONEY_TABLES:
        op.create_index(
            f"idx_{table_name}_exchange_account_env",
            table_name,
            ["exchange_account_id", "deployment_environment"],
        )
    op.create_index(
        "idx_api_keys_exchange_account",
        "api_keys",
        ["exchange_account_id"],
    )
    op.create_index(
        "idx_user_configs_exchange_account",
        "user_configs",
        ["exchange_account_id"],
    )


def _assert_downgrade_safe(bind: sa.Connection) -> None:
    """Refuse downgrade while any identity or backfill data is present."""
    checks = [(table_name, f"SELECT count(*) FROM {table_name}") for table_name in _IDENTITY_TABLES]
    checks.extend(
        (
            table_name,
            f"SELECT count(*) FROM {table_name} WHERE exchange_account_id IS NOT NULL",
        )
        for table_name in _MONEY_TABLES
    )
    for table_name, statement in checks:
        count = int(bind.execute(sa.text(statement)).scalar_one())
        if count:
            raise RuntimeError(
                f"cannot downgrade exchange account identity: dependent identity data in {table_name}"
            )


def downgrade() -> None:
    _assert_downgrade_safe(op.get_bind())

    op.drop_index("idx_user_configs_exchange_account", table_name="user_configs")
    op.drop_index("idx_api_keys_exchange_account", table_name="api_keys")
    for table_name in reversed(_ENV_MONEY_TABLES):
        op.drop_index(f"idx_{table_name}_exchange_account_env", table_name=table_name)
    for table_name in reversed(_MONEY_TABLES):
        op.drop_column(table_name, "exchange_account_id")

    op.drop_index("uq_account_config_drafts_account", table_name="account_config_drafts")
    op.drop_table("legacy_account_realm_map")
    op.drop_table("account_config_drafts")
    op.drop_index(
        "idx_exchange_account_credentials_account",
        table_name="exchange_account_credentials",
    )
    op.drop_index(
        "uq_exchange_account_credentials_active_venue",
        table_name="exchange_account_credentials",
    )
    op.drop_table("exchange_account_credentials")
    op.drop_index(
        "idx_exchange_account_memberships_user",
        table_name="exchange_account_memberships",
    )
    op.drop_table("exchange_account_memberships")
    op.drop_index("idx_exchange_accounts_venue_status", table_name="exchange_accounts")
    op.drop_table("exchange_accounts")
