"""Record why an accepted snapshot cannot authorize, decided when the fence is set."""
import sqlalchemy as sa

from alembic import op

revision = "e5c9a3f10b62"
down_revision = "d1b7c2e4a305"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means the snapshot can authorize. Not backfilled for the same reason
    # covered_prefix_hash is not: the verdict has to come from the proof that ran
    # when the fence was set, and no such proof ran for older rows. Those rows are
    # already unusable because their covered_prefix_hash is NULL.
    op.add_column(
        "capital_snapshots",
        sa.Column("authorization_blocked_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("capital_snapshots", "authorization_blocked_reason")
