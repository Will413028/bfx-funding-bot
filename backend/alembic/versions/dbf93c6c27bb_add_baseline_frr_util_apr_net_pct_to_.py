"""add baseline_frr_util_apr_net_pct to attribution_weekly

Revision ID: dbf93c6c27bb
Revises: 959586482e3b
Create Date: 2026-07-10 08:14:40.881843

Adds the utilization-adjusted AlwaysFRR baseline column (idealized
baseline_frr_apr_net_pct × mean market utilization). Autogenerate also
proposed dropping the intentional DB-only FKs fk_api_keys_user_profile /
fk_user_configs_user_profile / fk_user_profiles_user (pre-existing drift,
unrelated to this column — see 959586482e3b's note); stripped here.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dbf93c6c27bb"
down_revision: str | Sequence[str] | None = "959586482e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attribution_weekly",
        sa.Column("baseline_frr_util_apr_net_pct", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attribution_weekly", "baseline_frr_util_apr_net_pct")
