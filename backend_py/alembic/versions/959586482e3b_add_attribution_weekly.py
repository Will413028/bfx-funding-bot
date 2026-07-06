"""add attribution_weekly

Revision ID: 959586482e3b
Revises: 028cf1a1554e
Create Date: 2026-07-06 23:45:10.663926

Creates public.attribution_weekly — the E3 (b) per-cell weekly fee-adjusted
realized-APR read model (SoT is event_log; this table is rebuilt weekly by
scripts/run_weekly_attribution.py). GRANT SELECT to bfx_webapi is a separate
psql runbook step (E3 Task 8).

Autogenerate additionally proposed dropping the intentional DB-only FKs
fk_api_keys_user_profile / fk_user_configs_user_profile / fk_user_profiles_user
(the models declare no model-level ForeignKey — see accounts/tables.py); those
are correct and must stay, so they were stripped from this migration (mirrors
028cf1a1554e's note).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "959586482e3b"
down_revision: str | Sequence[str] | None = "028cf1a1554e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attribution_weekly",
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("cell", sa.Text(), nullable=False),
        sa.Column(
            "week_start_ms",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("week_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("n_fills", sa.Integer(), nullable=False),
        sa.Column("gross_interest_usdt", sa.Numeric(), nullable=False),
        sa.Column("net_interest_usdt", sa.Numeric(), nullable=False),
        sa.Column("capital_days", sa.Numeric(), nullable=False),
        sa.Column("realized_apr_net_pct", sa.Numeric(), nullable=True),
        sa.Column("baseline_close_apr_net_pct", sa.Numeric(), nullable=True),
        sa.Column("baseline_frr_apr_net_pct", sa.Numeric(), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "deployment_environment", "account_id", "cell", "week_start_ms"
        ),
    )


def downgrade() -> None:
    op.drop_table("attribution_weekly")
