"""funding interest payments

Revises 5b9e3d7a2f41. Realized income measured from the venue ledger
(category 28) instead of inferred from credit events, whose rate/period were
parsed one slot late until 2026-09-27. One row per daily payout, keyed by the
venue ledger id; the bot appends (``InterestLedgerSync``), the web API reads.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "3c8f1e6a9d52"
down_revision = "5b9e3d7a2f41"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"


def upgrade() -> None:
    op.create_table(
        "funding_interest_payments",
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("mts", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("balance", sa.Numeric(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("exchange_account_id", "ledger_id"),
    )
    op.execute("REVOKE ALL ON public.funding_interest_payments FROM PUBLIC")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.funding_interest_payments FROM bfx_bot;
        GRANT SELECT, INSERT ON public.funding_interest_payments TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.funding_interest_payments FROM bfx_webapi;
        GRANT SELECT ON public.funding_interest_payments TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.funding_interest_payments FROM bfx_webauth;
      END IF; END $$""")


def downgrade() -> None:
    op.drop_table("funding_interest_payments")
