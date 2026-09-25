"""Persist the loss limiter's 24h NAV window so a restart does not forget a loss (T9).

Revision ID: 9391a0f675d3
Revises: c3a639388457

ReconcileNavTracker computed realized_loss_pct_24h from an in-memory window,
so a restart right after a loss rebuilt the window from the post-loss NAV and
the limiter read zero. The daemon now appends a sample when NAV changes (or
every few minutes), reloads the last 24h at boot and prunes rows older than
two days. The runtime role may read, insert and prune; nothing else.

MERGE NOTE: cut from c3a639388457 in parallel with T5's release-governance
migrations; re-chain down_revision onto their head when the branches meet.
"""
import sqlalchemy as sa

from alembic import op

revision = "9391a0f675d3"
down_revision = "c3a639388457"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nav_window_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange_account_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("nav", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(["exchange_account_id"], ["exchange_accounts.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_nav_window_samples_scope_symbol_time", "nav_window_samples",
        ["exchange_account_id", "deployment_environment", "symbol", "occurred_at_ms"],
    )
    op.execute("REVOKE ALL ON public.nav_window_samples FROM PUBLIC")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_bot') THEN
        REVOKE ALL ON public.nav_window_samples FROM bfx_bot;
        GRANT SELECT, INSERT, DELETE ON public.nav_window_samples TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE public.nav_window_samples_id_seq TO bfx_bot;
      END IF; END $$""")


def downgrade() -> None:
    op.drop_index("ix_nav_window_samples_scope_symbol_time", table_name="nav_window_samples")
    op.drop_table("nav_window_samples")
