"""add venue offer type

Revision ID: de6f708192a3
Revises: cd5e6f708192
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "de6f708192a3"
down_revision: str | Sequence[str] | None = "cd5e6f708192"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("venue_offer_state", sa.Column("offer_type", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("venue_offer_state", "offer_type")
