"""add trading_halt

Revision ID: d437f9d4e3fe
Revises: c5a7e2d94f18
Create Date: 2026-07-27 15:11:08.939834

Persisted kill switch. Before it, the canary halt lived only in
BFX_KILL_SWITCH inside canary.env, so a plain revert of that file would have
resumed real-money trading silently.

NOTE: autogenerate also emitted drop_constraint for three Better Auth foreign
keys (fk_api_keys_user_profile, fk_user_configs_user_profile,
fk_user_profiles_user). Those tables are owned by the frontend's Better Auth
schema and are deliberately absent from this app's metadata, so autogenerate
reads them as "removed". They were stripped by hand — applying them would have
dropped real referential integrity from the production database. Re-check for
this on every future autogenerate against a live DB.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd437f9d4e3fe'
down_revision: Union[str, Sequence[str], None] = 'c5a7e2d94f18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'trading_halt',
        sa.Column(
            'id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'),
            autoincrement=True, nullable=False,
        ),
        sa.Column('account_id', sa.Text(), nullable=False),
        sa.Column('deployment_environment', sa.Text(), nullable=False),
        sa.Column('halted', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('actor', sa.Text(), nullable=False),
        sa.Column('created_at_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_trading_halt_realm_id', 'trading_halt',
        ['account_id', 'deployment_environment', 'id'], unique=False,
    )
    # The daemon reads this table on the submit path under the bot role; the
    # web API never does. No extra GRANT needed for bfx_webapi.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_trading_halt_realm_id', table_name='trading_halt')
    op.drop_table('trading_halt')
