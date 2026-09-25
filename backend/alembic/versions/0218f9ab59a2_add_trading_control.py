"""Material-deploy approval, operator resume requests and the probation floor.

ADR 2026-09-25 D2/D3: a material deploy parks the writer in REDUCING until an
operator approves that exact image once; the approval (and a resume after an
automatic HALTED) starts a probation period at 25% of the normal cell limit,
never below one venue-minimum offer.

- ``deployment_approvals``: append-only, one row per approved backend digest.
  The ``deployments`` ledger is append-only and written by the deploy tool, so
  an approval cannot be recorded on it after the fact.
- ``trading_control_requests``: the web API's only write. It inserts a request
  (approve or resume) with the operator's identity; the account daemon applies
  it under the account lock and records one outcome. The web API has no write
  on trading_state or on the approvals (ADR D4': zero write on execution state).
- ``trading_state.probation_floor``: the per-currency venue minimum (native
  units, with the submit margin) observed when probation starts, so every
  capital read applies the same floor without a venue call.
- ``trading_operator_authorized``: the operator check the daemon re-runs when it
  applies a request; independent of the release ceremony's function, which is
  removed with that ceremony.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0218f9ab59a2"
down_revision = "c3a639388457"
branch_labels = None
depends_on = None

_DIGEST = "'^sha256:[0-9a-f]{64}$'"
_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,action,backend_digest,"
    "reason,requested_by,created_at_ms"
)
_WORKER_COLUMNS = "state,processed_at_ms,outcome_reason,trading_state_id"

_OLD_PROBATION_CHECK = (
    "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL) OR "
    "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
    "AND probation_started_at_ms >= 0)"
)
_PROBATION_CHECK = (
    "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL "
    "AND probation_floor IS NULL) OR "
    "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
    "AND probation_started_at_ms >= 0 AND probation_floor IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column("trading_state", sa.Column("probation_floor", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_trading_state_probation", "trading_state", type_="check")
    op.create_check_constraint("ck_trading_state_probation", "trading_state", _PROBATION_CHECK)

    op.create_table(
        "deployment_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_deployment_approvals_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("backend_digest", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(f"backend_digest ~ {_DIGEST}", name="ck_deployment_approvals_digest"),
        sa.CheckConstraint("source_revision ~ '^[0-9a-f]{40}$'",
                           name="ck_deployment_approvals_revision"),
        sa.CheckConstraint("length(trim(approved_by)) > 0 AND approved_at_ms >= 0",
                           name="ck_deployment_approvals_evidence"),
        sa.UniqueConstraint("exchange_account_id", "deployment_environment", "backend_digest",
                            name="uq_deployment_approvals_digest"),
    )

    op.create_table(
        "trading_control_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_trading_control_requests_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("backend_digest", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("trading_state_id", sa.BigInteger(),
                  sa.ForeignKey("trading_state.id", ondelete="RESTRICT",
                                name="fk_trading_control_requests_trading_state"), nullable=True),
        sa.CheckConstraint("action IN ('approve', 'resume')", name="ck_trading_control_requests_action"),
        sa.CheckConstraint(f"backend_digest ~ {_DIGEST}", name="ck_trading_control_requests_digest"),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500 AND length(trim(requested_by)) > 0 "
            "AND created_at_ms >= 0",
            name="ck_trading_control_requests_evidence",
        ),
        sa.CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL "
            "AND trading_state_id IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL AND trading_state_id IS NULL)",
            name="ck_trading_control_requests_outcome",
        ),
    )
    op.create_index(
        "uq_trading_control_requests_pending", "trading_control_requests",
        ["exchange_account_id", "deployment_environment"], unique=True,
        postgresql_where=sa.text("state = 'requested'"),
    )
    op.create_index(
        "ix_trading_control_requests_queue", "trading_control_requests",
        ["exchange_account_id", "deployment_environment", "state", "created_at_ms"],
    )

    op.execute("""CREATE FUNCTION public.reject_trading_control_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'immutable trading control history'; END $$""")
    op.execute("CREATE TRIGGER deployment_approvals_history BEFORE UPDATE OR DELETE "
               "ON public.deployment_approvals FOR EACH ROW "
               "EXECUTE FUNCTION public.reject_trading_control_mutation()")
    op.execute("CREATE TRIGGER deployment_approvals_no_truncate BEFORE TRUNCATE "
               "ON public.deployment_approvals FOR EACH STATEMENT "
               "EXECUTE FUNCTION public.reject_trading_control_mutation()")
    # What the operator asked for is fixed once written; the daemon records one
    # terminal outcome and nothing rewrites it.
    request_columns = _REQUEST_COLUMNS.split(",")
    op.execute(f"""CREATE FUNCTION public.guard_trading_control_request() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in request_columns)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in request_columns)})
          THEN RAISE EXCEPTION 'immutable trading control request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid trading control request transition'; END IF;
        RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER trading_control_request_transition BEFORE UPDATE "
               "ON public.trading_control_requests FOR EACH ROW "
               "EXECUTE FUNCTION public.guard_trading_control_request()")
    op.execute("CREATE TRIGGER trading_control_request_no_delete BEFORE DELETE "
               "ON public.trading_control_requests FOR EACH ROW "
               "EXECUTE FUNCTION public.reject_trading_control_mutation()")
    op.execute("CREATE TRIGGER trading_control_request_no_truncate BEFORE TRUNCATE "
               "ON public.trading_control_requests FOR EACH STATEMENT "
               "EXECUTE FUNCTION public.reject_trading_control_mutation()")

    # Same authority as the release ceremony's check, owned by trading control
    # so removing the ceremony does not remove it.
    op.execute('''CREATE FUNCTION public.trading_operator_authorized(account uuid, actor text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT EXISTS (SELECT 1 FROM auth."user" u
          JOIN public.exchange_account_memberships m ON m.user_id=u.id
          JOIN public.exchange_accounts a ON a.id=m.exchange_account_id
          WHERE a.id=account AND u.id=actor AND u.role='admin' AND u.banned IS NOT TRUE
            AND u."twoFactorEnabled" IS TRUE AND m.role IN ('owner','operator')
            AND a.lifecycle_status IN ('active','halted') FOR SHARE OF u,m,a)
        $$''')

    op.execute("REVOKE ALL ON FUNCTION public.reject_trading_control_mutation(), "
               "public.guard_trading_control_request(), "
               "public.trading_operator_authorized(uuid,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON public.deployment_approvals, public.trading_control_requests FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE public.deployment_approvals_id_seq FROM PUBLIC")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.deployment_approvals, public.trading_control_requests FROM bfx_bot;
        REVOKE ALL ON SEQUENCE public.deployment_approvals_id_seq FROM bfx_bot;
        GRANT SELECT, INSERT ON public.deployment_approvals TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE public.deployment_approvals_id_seq TO bfx_bot;
        GRANT SELECT ON public.trading_control_requests TO bfx_bot;
        GRANT UPDATE ({_WORKER_COLUMNS}) ON public.trading_control_requests TO bfx_bot;
        GRANT EXECUTE ON FUNCTION public.trading_operator_authorized(uuid,text) TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.deployment_approvals, public.trading_control_requests FROM bfx_webapi;
        REVOKE ALL ON SEQUENCE public.deployment_approvals_id_seq FROM bfx_webapi;
        GRANT SELECT ON public.deployment_approvals, public.trading_control_requests TO bfx_webapi;
        GRANT INSERT ({_REQUEST_COLUMNS}) ON public.trading_control_requests TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.deployment_approvals, public.trading_control_requests FROM bfx_webauth;
      END IF;
    END $$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.deployment_approvals)
           OR EXISTS (SELECT FROM public.trading_control_requests)
           OR EXISTS (SELECT FROM public.trading_state WHERE probation_floor IS NOT NULL) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded approvals, requests or probation';
        END IF; END $$""")
    op.drop_table("trading_control_requests")
    op.drop_table("deployment_approvals")
    op.execute("DROP FUNCTION public.guard_trading_control_request(), "
               "public.reject_trading_control_mutation(), "
               "public.trading_operator_authorized(uuid,text)")
    op.drop_constraint("ck_trading_state_probation", "trading_state", type_="check")
    op.create_check_constraint("ck_trading_state_probation", "trading_state", _OLD_PROBATION_CHECK)
    op.drop_column("trading_state", "probation_floor")
