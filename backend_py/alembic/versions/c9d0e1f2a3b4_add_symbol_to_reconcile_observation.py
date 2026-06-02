"""add symbol to reconcile_observation

Revision ID: c9d0e1f2a3b4
Revises: b7c1d2e3f4a5
Create Date: 2026-06-02

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: str | Sequence[str] | None = 'b7c1d2e3f4a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'reconcile_observation',
        sa.Column('symbol', sa.Text(), nullable=False, server_default=sa.text("'fUST'")),
    )
    op.create_index(
        'idx_reconcile_obs_acct_env_symbol_id',
        'reconcile_observation',
        ['account_id', 'deployment_environment', 'symbol', 'id'],
    )
    op.drop_index('idx_reconcile_obs_acct_env_id', table_name='reconcile_observation')


def downgrade() -> None:
    op.create_index(
        'idx_reconcile_obs_acct_env_id',
        'reconcile_observation',
        ['account_id', 'deployment_environment', 'id'],
    )
    op.drop_index('idx_reconcile_obs_acct_env_symbol_id', table_name='reconcile_observation')
    op.drop_column('reconcile_observation', 'symbol')
