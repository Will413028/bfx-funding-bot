"""enforce unique non-null offer claim reservation identities.

Revision ID: a6c9e2f4b7d1
Revises: f5b8d0e2f3a4
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6c9e2f4b7d1"
down_revision: str | Sequence[str] | None = "f5b8d0e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_offer_claims_venue_offer_id",
        "offer_claims",
        ["account_id", "deployment_environment", "venue_offer_id"],
        unique=True,
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
        sqlite_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.create_index(
        "uq_offer_claims_execution_decision_id",
        "offer_claims",
        ["account_id", "deployment_environment", "execution_decision_id"],
        unique=True,
        postgresql_where=sa.text("execution_decision_id IS NOT NULL"),
        sqlite_where=sa.text("execution_decision_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_offer_claims_execution_decision_id", table_name="offer_claims")
    op.drop_index("uq_offer_claims_venue_offer_id", table_name="offer_claims")
