"""funding credit history and funding trades

Revises 3c8f1e6a9d52. Per-credit truth for per-cell attribution: ended credits
and loans from the venue's history (rate, period, opening, actual close) and the
funding trades that link each credit to the offer, and so the cell, that
placed it. Both are keyed by venue ids; the bot appends (``CreditHistorySync``),
the web API reads.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "9a4d6e2c7b18"
down_revision = "3c8f1e6a9d52"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_TABLES = ("funding_credit_history", "funding_trades")


def upgrade() -> None:
    op.create_table(
        "funding_credit_history",
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("credit_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Integer(), nullable=True),
        sa.Column("mts_create", sa.BigInteger(), nullable=False),
        sa.Column("mts_update", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("mts_opening", sa.BigInteger(), nullable=False),
        sa.Column("mts_last_payout", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("exchange_account_id", "kind", "credit_id"),
        sa.CheckConstraint("kind IN ('credit', 'loan')", name="ck_funding_credit_history_kind"),
    )
    op.create_table(
        "funding_trades",
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("mts_create", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("maker", sa.Boolean(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True),
                  server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("exchange_account_id", "trade_id"),
    )
    for table in _TABLES:
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        op.execute(f"""DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
            REVOKE ALL ON public.{table} FROM bfx_bot;
            GRANT SELECT, INSERT ON public.{table} TO bfx_bot;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
            REVOKE ALL ON public.{table} FROM bfx_webapi;
            GRANT SELECT ON public.{table} TO bfx_webapi;
          END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
            REVOKE ALL ON public.{table} FROM bfx_webauth;
          END IF; END $$""")


def downgrade() -> None:
    op.drop_table("funding_trades")
    op.drop_table("funding_credit_history")
