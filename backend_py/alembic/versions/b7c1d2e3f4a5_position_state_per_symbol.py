"""position_state per-symbol: add symbol to PK, rename reserved_usdt/realized_usdt

Revision ID: b7c1d2e3f4a5
Revises: a3ae9a60862a
Create Date: 2026-05-31 00:00:00.000000

Pre-launch clean recreate: position_state is a derived snapshot (rebuildable
from event_log + reconcile_observation), and there is no production data worth
preserving, so this DROPs and reCREATEs the table with the new composite PK
(account_id, deployment_environment, symbol) and native-unit column names
reserved/realized. No backfill. The downgrade restores the legacy single-tenant
PK and *_usdt names (also a clean recreate — symbol history is not recoverable).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c1d2e3f4a5'
down_revision: str | Sequence[str] | None = 'a3ae9a60862a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Recreate position_state with the per-symbol PK + renamed columns."""
    op.drop_table('position_state')
    op.create_table(
        'position_state',
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('deployment_environment', sa.Text(), nullable=False),
        sa.Column('symbol', sa.Text(), nullable=False),
        sa.Column('reserved', sa.Numeric(), server_default=sa.text('0'), nullable=False),
        sa.Column('realized', sa.Numeric(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_updated_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_event_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_reconciled_at', sa.BigInteger(), nullable=True),
        sa.Column('n_credits', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('account_id', 'deployment_environment', 'symbol'),
    )


def downgrade() -> None:
    """Restore the legacy single-tenant position_state (no symbol, *_usdt names)."""
    op.drop_table('position_state')
    op.create_table(
        'position_state',
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('deployment_environment', sa.Text(), nullable=False),
        sa.Column('reserved_usdt', sa.Numeric(), server_default=sa.text('0'), nullable=False),
        sa.Column('realized_usdt', sa.Numeric(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_updated_ms', sa.BigInteger(), nullable=False),
        sa.Column('last_event_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_reconciled_at', sa.BigInteger(), nullable=True),
        sa.Column('n_credits', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('account_id', 'deployment_environment'),
    )
