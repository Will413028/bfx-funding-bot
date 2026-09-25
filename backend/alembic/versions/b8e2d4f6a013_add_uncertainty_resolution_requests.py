"""Operator adjudication outbox; the web API loses every ledger write (ADR D4').

`AccountEventWriter.append` projects synchronously, so the synchronous
adjudication endpoints needed write access to the ledger and every read model a
resolution touches -- five grants were added by hand on 2026-09-22 to unblock a
canary and never existed in any versioned artifact. From this revision the web
API inserts a request row and the account daemon applies it. The same revision
takes those writes back, so the rollback is reproducible instead of a runbook
step someone has to remember.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b8e2d4f6a013"
down_revision = "0218f9ab59a2"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "reconcile_event_seq,venue_offer_id,decision,reason,requested_by,created_at_ms"
)
_WORKER_COLUMNS = "state,processed_at_ms,resolved_event_seq,outcome_reason"
# The ledger, its projections and the other execution tables. The web API keeps
# only the SELECTs the cutover runbook (6b) grants; it writes none of these.
_NO_WEBAPI_WRITE = (
    "event_log", "event_prefix_hashes", "projection_heads",
    "execution_uncertainties", "offer_claims", "position_state", "reconcile_observation",
    "submission_attempts", "venue_credit_state", "venue_offer_state",
    "capital_snapshots", "capital_snapshot_queries", "capital_policy_revisions",
    "capital_policy_heads", "execution_decisions", "canary_command_permits", "trading_halt",
)
# Hand-granted on 2026-09-22 for the synchronous append and never part of the
# web API's read baseline.
_NO_WEBAPI_ACCESS = ("event_prefix_hashes", "projection_heads")


def upgrade() -> None:
    op.create_table(
        "uncertainty_resolution_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "exchange_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "exchange_accounts.id",
                ondelete="RESTRICT",
                name="fk_uncertainty_resolution_requests_account",
            ),
            nullable=False,
        ),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("uncertainty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reconcile_event_seq", sa.BigInteger(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column(
            "resolved_event_seq",
            sa.BigInteger(),
            sa.ForeignKey(
                "event_log.event_seq",
                ondelete="RESTRICT",
                name="fk_uncertainty_resolution_requests_event",
            ),
            nullable=True,
        ),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "action IN ('bind_to_venue', 'mark_not_accepted', 'manual_resolution')",
            name="ck_uncertainty_resolution_requests_action",
        ),
        sa.CheckConstraint(
            "state IN ('requested', 'applied', 'rejected', 'failed')",
            name="ck_uncertainty_resolution_requests_state",
        ),
        sa.CheckConstraint(
            "(action = 'bind_to_venue' AND venue_offer_id IS NOT NULL AND decision IS NULL) OR "
            "(action = 'mark_not_accepted' AND venue_offer_id IS NULL AND decision IS NULL) OR "
            "(action = 'manual_resolution' AND venue_offer_id IS NULL AND decision IS NOT NULL "
            "AND reason IS NOT NULL)",
            name="ck_uncertainty_resolution_requests_action_shape",
        ),
        sa.CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND resolved_event_seq IS NULL "
            "AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL AND resolved_event_seq IS NOT NULL "
            "AND outcome_reason IS NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND resolved_event_seq IS NULL AND outcome_reason IS NOT NULL)",
            name="ck_uncertainty_resolution_requests_outcome_shape",
        ),
    )
    op.create_index(
        "uq_uncertainty_resolution_requests_pending",
        "uncertainty_resolution_requests",
        ["exchange_account_id", "deployment_environment", "uncertainty_id"],
        unique=True,
        postgresql_where=sa.text("state = 'requested'"),
    )
    op.create_index(
        "ix_uncertainty_resolution_requests_queue",
        "uncertainty_resolution_requests",
        ["exchange_account_id", "deployment_environment", "state", "created_at_ms"],
    )
    # What the operator asked for is fixed once written; the worker records one
    # terminal outcome and nothing rewrites it.
    op.execute(f"""CREATE FUNCTION public.guard_uncertainty_resolution_request() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in _REQUEST_COLUMNS.split(','))})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in _REQUEST_COLUMNS.split(','))})
          THEN RAISE EXCEPTION 'immutable uncertainty resolution request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid uncertainty resolution transition'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE FUNCTION public.reject_uncertainty_resolution_request_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'immutable uncertainty resolution history'; END $$""")
    op.execute("""CREATE TRIGGER uncertainty_resolution_request_transition
        BEFORE UPDATE ON public.uncertainty_resolution_requests
        FOR EACH ROW EXECUTE FUNCTION public.guard_uncertainty_resolution_request()""")
    op.execute("""CREATE TRIGGER uncertainty_resolution_request_no_delete
        BEFORE DELETE ON public.uncertainty_resolution_requests
        FOR EACH ROW EXECUTE FUNCTION public.reject_uncertainty_resolution_request_mutation()""")
    op.execute("""CREATE TRIGGER uncertainty_resolution_request_no_truncate
        BEFORE TRUNCATE ON public.uncertainty_resolution_requests
        FOR EACH STATEMENT EXECUTE FUNCTION public.reject_uncertainty_resolution_request_mutation()""")
    op.execute("REVOKE ALL ON FUNCTION public.guard_uncertainty_resolution_request(), "
               "public.reject_uncertainty_resolution_request_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.uncertainty_resolution_requests FROM PUBLIC")
    no_write = ",".join(f"'{table}'" for table in _NO_WEBAPI_WRITE)
    no_access = ",".join(f"'{table}'" for table in _NO_WEBAPI_ACCESS)
    op.execute(f"""DO $$ DECLARE t text; BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.uncertainty_resolution_requests FROM bfx_webapi;
        GRANT SELECT ON public.uncertainty_resolution_requests TO bfx_webapi;
        GRANT INSERT ({_REQUEST_COLUMNS}) ON public.uncertainty_resolution_requests TO bfx_webapi;
        FOREACH t IN ARRAY ARRAY[{no_write}] LOOP
          IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM bfx_webapi', t);
          END IF;
        END LOOP;
        FOREACH t IN ARRAY ARRAY[{no_access}] LOOP
          IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON public.%I FROM bfx_webapi', t);
          END IF;
        END LOOP;
        IF to_regclass('public.event_log_event_seq_seq') IS NOT NULL THEN
          REVOKE ALL ON SEQUENCE public.event_log_event_seq_seq FROM bfx_webapi;
        END IF;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.uncertainty_resolution_requests FROM bfx_bot;
        GRANT SELECT ON public.uncertainty_resolution_requests TO bfx_bot;
        GRANT UPDATE ({_WORKER_COLUMNS}) ON public.uncertainty_resolution_requests TO bfx_bot;
      END IF; END $$""")


def downgrade() -> None:
    # The revoked web API writes are deliberately not restored: the previous
    # build's synchronous path needs them, and granting them back is a decision
    # for whoever downgrades, not a side effect of it.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM public.uncertainty_resolution_requests) THEN
        RAISE EXCEPTION 'refuse downgrade of populated uncertainty resolution requests'; END IF; END $$""")
    op.drop_table("uncertainty_resolution_requests")
    op.execute("DROP FUNCTION public.guard_uncertainty_resolution_request(), "
               "public.reject_uncertainty_resolution_request_mutation()")
