"""Bind each capital snapshot to the ledger prefix it was derived from."""
import sqlalchemy as sa

from alembic import op

revision = "d1b7c2e4a305"
down_revision = "c3f5a1d7e204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately not backfilled. The value must be the prefix hash observed when
    # the classification was derived; copying today's event_log.prefix_hash into an
    # older row would assert a binding nobody checked at the time. A read treats
    # NULL as unproven and blocks, and the next accepted snapshot supplies it --
    # reconcile accepts one roughly every 90s.
    op.add_column(
        "capital_snapshots", sa.Column("covered_prefix_hash", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("capital_snapshots", "covered_prefix_hash")
