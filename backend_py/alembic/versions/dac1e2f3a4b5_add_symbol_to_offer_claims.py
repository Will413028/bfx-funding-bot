"""add symbol to offer_claims

Revision ID: dac1e2f3a4b5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-02

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dac1e2f3a4b5'
down_revision: str | Sequence[str] | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'offer_claims',
        sa.Column('symbol', sa.Text(), nullable=False, server_default=sa.text("'fUST'")),
    )


def downgrade() -> None:
    op.drop_column('offer_claims', 'symbol')
