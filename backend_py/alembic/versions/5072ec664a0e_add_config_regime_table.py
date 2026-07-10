"""add config_regime table

Revision ID: 5072ec664a0e
Revises: dbf93c6c27bb
Create Date: 2026-07-10 08:31:05.386515

Creates public.config_regime — one best-effort row per daemon boot recording
the execution-policy flags (clamp_enabled, reprice_enabled) in effect for
that boot. Flag flips require a restart (config is boot-immutable), so boots
ARE the regime boundaries; Task 5's report script reads this table to
attribute execution-quality metrics to the flag state that produced them.
Telemetry, not SoT — prunable.

Autogenerate additionally proposed dropping the intentional DB-only FKs
fk_api_keys_user_profile / fk_user_configs_user_profile / fk_user_profiles_user
(pre-existing drift, unrelated to this table — see 959586482e3b's note);
stripped here.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5072ec664a0e"
down_revision: str | Sequence[str] | None = "dbf93c6c27bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_regime",
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at_ms",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("clamp_enabled", sa.Boolean(), nullable=False),
        sa.Column("reprice_enabled", sa.Boolean(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "deployment_environment", "account_id", "recorded_at_ms"
        ),
    )


def downgrade() -> None:
    op.drop_table("config_regime")
