"""add api_key vault (SP2)

Revision ID: f1a2b3c4d5e6
Revises: e61f3d1ed7ca
Create Date: 2026-06-07

Creates public.api_keys (envelope-encrypted Bitfinex secrets) with a DB-level FK
to user_profiles.user_id. PG requires the FK target to be a UNIQUE CONSTRAINT
(a bare UNIQUE INDEX is insufficient), so this migration replaces SP1's
idx_user_profiles_user_id unique index with a uq_user_profiles_user_id unique
constraint. Hand-crafted (live Neon: no autogenerate); applied on the VM via the
migrate compose service. GRANTs to bfx_webapi are a separate psql runbook step.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e61f3d1ed7ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # FK target must be a unique constraint, not just a unique index.
    op.drop_index(
        "idx_user_profiles_user_id", table_name="user_profiles", schema="public"
    )
    op.create_unique_constraint(
        "uq_user_profiles_user_id", "user_profiles", ["user_id"], schema="public"
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("dek_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "exchange_status", sa.Text(), nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_profiles.user_id"],
            ondelete="CASCADE", name="fk_api_keys_user_profile",
        ),
        schema="public",
    )
    op.create_index(
        "idx_api_keys_user_id", "api_keys", ["user_id"], unique=True, schema="public"
    )


def downgrade() -> None:
    op.drop_index("idx_api_keys_user_id", table_name="api_keys", schema="public")
    op.drop_table("api_keys", schema="public")
    op.drop_constraint(
        "uq_user_profiles_user_id", "user_profiles", schema="public", type_="unique"
    )
    op.create_index(
        "idx_user_profiles_user_id", "user_profiles", ["user_id"],
        unique=True, schema="public",
    )
