"""add user_configs (SP3)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-17

Creates public.user_configs (one inert strategy-preference config per user) with
a DB-level FK to user_profiles.user_id (which carries uq_user_profiles_user_id).
Hand-crafted (live Neon: no autogenerate); applied on the VM via the migrate
compose service. GRANT to bfx_webapi is a separate psql runbook step.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql

from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_configs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("config", sa.dialects.postgresql.JSONB(), nullable=False),
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
            ondelete="CASCADE", name="fk_user_configs_user_profile",
        ),
        schema="public",
    )
    op.create_index(
        "idx_user_configs_user_id", "user_configs", ["user_id"],
        unique=True, schema="public",
    )


def downgrade() -> None:
    op.drop_index("idx_user_configs_user_id", table_name="user_configs", schema="public")
    op.drop_table("user_configs", schema="public")
