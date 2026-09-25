"""Durable record of every venue funding cancel-all the kill switch issues.

Entering HALTED writes the trading state first and then asks the venue to
cancel every funding offer, currency by currency. The trading state says the
writer must not place anything; this table says what the venue was actually
asked to do and what it answered, so a kill that did not fully land is visible
and can be retried rather than assumed.

One row per phase: ``requested`` is written before the call (so a crash mid-call
leaves evidence), then exactly one terminal row -- ``acknowledged``,
``rejected``, ``failed`` or ``skipped`` (no call was made, e.g. the writer lock
was not held). Rows are never updated or deleted; a retry is a new attempt.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c2b7b04da604"
down_revision = "8e4f33517b10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "funding_cancel_all_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "exchange_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                          name="fk_funding_cancel_all_audit_account"),
            nullable=False,
        ),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        # The HALTED decision this cancel-all enforces.
        sa.Column(
            "trading_state_id",
            sa.BigInteger(),
            sa.ForeignKey("trading_state.id", ondelete="RESTRICT",
                          name="fk_funding_cancel_all_audit_trading_state"),
            nullable=False,
        ),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("venue_status", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "phase IN ('requested', 'acknowledged', 'rejected', 'failed', 'skipped')",
            name="ck_funding_cancel_all_audit_phase",
        ),
        sa.CheckConstraint(
            "(phase = 'requested' AND venue_status IS NULL AND detail IS NULL) OR phase <> 'requested'",
            name="ck_funding_cancel_all_audit_request_shape",
        ),
        sa.CheckConstraint(
            "length(currency) BETWEEN 1 AND 16 AND length(trim(actor)) > 0 "
            "AND (detail IS NULL OR length(detail) <= 512) "
            "AND (venue_status IS NULL OR length(venue_status) <= 64) AND occurred_at_ms >= 0",
            name="ck_funding_cancel_all_audit_evidence",
        ),
    )
    op.create_index(
        "ix_funding_cancel_all_audit_scope_id", "funding_cancel_all_audit",
        ["exchange_account_id", "deployment_environment", "id"],
    )
    # One request and at most one outcome per attempt.
    op.create_index(
        "uq_funding_cancel_all_audit_request", "funding_cancel_all_audit", ["attempt_id"],
        unique=True, postgresql_where=sa.text("phase = 'requested'"),
    )
    op.create_index(
        "uq_funding_cancel_all_audit_outcome", "funding_cancel_all_audit", ["attempt_id"],
        unique=True, postgresql_where=sa.text("phase <> 'requested'"),
    )
    op.execute("""CREATE FUNCTION public.reject_funding_cancel_all_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'immutable funding cancel-all audit'; END $$""")
    op.execute("CREATE TRIGGER funding_cancel_all_audit_history BEFORE UPDATE OR DELETE "
               "ON public.funding_cancel_all_audit FOR EACH ROW "
               "EXECUTE FUNCTION public.reject_funding_cancel_all_audit_mutation()")
    op.execute("CREATE TRIGGER funding_cancel_all_audit_no_truncate BEFORE TRUNCATE "
               "ON public.funding_cancel_all_audit FOR EACH STATEMENT "
               "EXECUTE FUNCTION public.reject_funding_cancel_all_audit_mutation()")
    op.execute("REVOKE ALL ON FUNCTION public.reject_funding_cancel_all_audit_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.funding_cancel_all_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE public.funding_cancel_all_audit_id_seq FROM PUBLIC")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.funding_cancel_all_audit FROM bfx_bot;
        REVOKE ALL ON SEQUENCE public.funding_cancel_all_audit_id_seq FROM bfx_bot;
        GRANT SELECT, INSERT ON public.funding_cancel_all_audit TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE public.funding_cancel_all_audit_id_seq TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.funding_cancel_all_audit FROM bfx_webapi;
        REVOKE ALL ON SEQUENCE public.funding_cancel_all_audit_id_seq FROM bfx_webapi;
        GRANT SELECT ON public.funding_cancel_all_audit TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.funding_cancel_all_audit FROM bfx_webauth;
        REVOKE ALL ON SEQUENCE public.funding_cancel_all_audit_id_seq FROM bfx_webauth;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.funding_cancel_all_audit) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded funding cancel-all attempts';
        END IF; END $$""")
    op.drop_table("funding_cancel_all_audit")
    op.execute("DROP FUNCTION public.reject_funding_cancel_all_audit_mutation()")
