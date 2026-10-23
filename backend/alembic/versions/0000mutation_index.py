"""mutation (reverted in the next commit): a blocking index squawk must refuse"""
from alembic import op

revision = "0000mutation"
down_revision = "3c5e7a9b1d2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_mutation ON alembic_version (version_num)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_mutation")
