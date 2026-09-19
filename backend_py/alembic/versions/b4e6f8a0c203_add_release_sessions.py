"""Release handoff and immutable audit; preserve every prior permit and halt."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b4e6f8a0c203"
down_revision = "a9d3e5f7b102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_decisions", sa.Column("strategy", sa.Text(), nullable=True))
    op.create_table("release_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("exchange_account_id", sa.Uuid(), sa.ForeignKey("exchange_accounts.id"), nullable=False),
        *(sa.Column(name, sa.Text(), nullable=False) for name in
          ("deployment_environment", "symbol", "cell", "strategy", "requested_by")),
        sa.Column("max_amount", sa.Numeric(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("requested_action", sa.Text(), nullable=False, server_default="prepare"),
        sa.Column("request_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("processed_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("binding", postgresql.JSONB(), nullable=True),
        sa.Column("halt_id", sa.BigInteger(), sa.ForeignKey("trading_halt.id")),
        sa.Column("minimum_amount", sa.Numeric()),
        sa.Column("authorized_by", sa.Text()),
        sa.Column("authorized_at_ms", sa.BigInteger()),
        sa.Column("consumed_at_ms", sa.BigInteger()),
        sa.Column("permit_id", sa.Uuid(), sa.ForeignKey("canary_command_permits.permit_id")),
        sa.Column("decision_id", sa.Text(), sa.ForeignKey("execution_decisions.decision_id")),
        sa.Column("attempt_id", sa.Uuid()),
        sa.Column("exact_amount", sa.Numeric()),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("promoted_halt_id", sa.BigInteger(), sa.ForeignKey("trading_halt.id")),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint("state IN ('requested','prepared','authorized','consumed','observed','validated','promoted','blocked')", name="ck_release_state"),
        sa.CheckConstraint("requested_action IN ('prepare','authorize','validate','promote')", name="ck_release_action"),
        sa.CheckConstraint("symbol = 'fUST' AND max_amount > 0 AND expires_at_ms > created_at_ms", name="ck_release_scope"),
        sa.CheckConstraint("CAST(max_amount AS TEXT) NOT IN ('NaN','Infinity','-Infinity')", name="ck_release_finite"),
        sa.CheckConstraint("state IN ('requested','blocked') OR (binding IS NOT NULL AND halt_id IS NOT NULL AND minimum_amount IS NOT NULL AND minimum_amount > 0 AND minimum_amount <= max_amount)", name="ck_release_prepared"),
        sa.CheckConstraint("state IN ('requested','prepared','blocked') OR (authorized_by IS NOT NULL AND authorized_at_ms IS NOT NULL)", name="ck_release_authorized"),
        sa.CheckConstraint("state NOT IN ('consumed','observed','validated','promoted') OR (permit_id IS NOT NULL AND decision_id IS NOT NULL AND attempt_id IS NOT NULL AND consumed_at_ms IS NOT NULL AND exact_amount IS NOT NULL)", name="ck_release_consumed"),
        sa.CheckConstraint("state NOT IN ('validated','promoted') OR evidence IS NOT NULL", name="ck_release_validated"),
        sa.CheckConstraint("state <> 'promoted' OR promoted_halt_id IS NOT NULL", name="ck_release_promoted"),
        sa.CheckConstraint("exact_amount IS NULL OR (exact_amount > 0 AND exact_amount <= max_amount)", name="ck_release_amount"),
        sa.CheckConstraint("request_revision >= processed_revision AND processed_revision >= 0", name="ck_release_revision"))
    op.create_table("release_session_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("release_sessions.id"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("occurred_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False))
    op.execute("""CREATE FUNCTION public.guard_release_session() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF (NEW.id,NEW.exchange_account_id,NEW.deployment_environment,NEW.symbol,NEW.cell,
            NEW.strategy,NEW.max_amount,NEW.expires_at_ms,NEW.created_at_ms)
          IS DISTINCT FROM (OLD.id,OLD.exchange_account_id,OLD.deployment_environment,OLD.symbol,OLD.cell,
            OLD.strategy,OLD.max_amount,OLD.expires_at_ms,OLD.created_at_ms)
          OR (OLD.binding IS NOT NULL AND NEW.binding IS DISTINCT FROM OLD.binding)
          OR (OLD.halt_id IS NOT NULL AND NEW.halt_id IS DISTINCT FROM OLD.halt_id)
          OR (OLD.minimum_amount IS NOT NULL AND NEW.minimum_amount IS DISTINCT FROM OLD.minimum_amount)
          OR (OLD.authorized_at_ms IS NOT NULL AND (NEW.authorized_at_ms,NEW.authorized_by)
             IS DISTINCT FROM (OLD.authorized_at_ms,OLD.authorized_by))
          OR (OLD.promoted_halt_id IS NOT NULL AND NEW.promoted_halt_id IS DISTINCT FROM OLD.promoted_halt_id)
          OR (OLD.consumed_at_ms IS NOT NULL AND
             (NEW.consumed_at_ms,NEW.permit_id,NEW.decision_id,NEW.attempt_id,NEW.exact_amount)
              IS DISTINCT FROM (OLD.consumed_at_ms,OLD.permit_id,OLD.decision_id,OLD.attempt_id,OLD.exact_amount))
          THEN RAISE EXCEPTION 'immutable release binding'; END IF;
        IF NEW.state <> OLD.state AND NOT (
          (OLD.state='requested' AND NEW.state='prepared') OR
          (OLD.state='prepared' AND NEW.state='authorized') OR
          (OLD.state='authorized' AND NEW.state='consumed') OR
          (OLD.state='consumed' AND NEW.state='observed') OR
          (OLD.state='observed' AND NEW.state='validated') OR
          (OLD.state='validated' AND NEW.state='promoted') OR
          (OLD.state <> 'blocked' AND NEW.state='blocked'))
          THEN RAISE EXCEPTION 'invalid release transition'; END IF;
        RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER release_transition BEFORE UPDATE ON public.release_sessions FOR EACH ROW EXECUTE FUNCTION public.guard_release_session()")
    op.execute("""CREATE FUNCTION public.reject_release_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'immutable release audit'; END $$""")
    op.execute("CREATE TRIGGER release_history BEFORE UPDATE OR DELETE ON public.release_session_audit FOR EACH ROW EXECUTE FUNCTION public.reject_release_mutation()")
    op.execute("CREATE TRIGGER release_no_delete BEFORE DELETE ON public.release_sessions FOR EACH ROW EXECUTE FUNCTION public.reject_release_mutation()")
    op.execute("""CREATE FUNCTION public.guard_release_permit() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF (NEW.permit_id,NEW.halt_id,NEW.exchange_account_id,NEW.deployment_environment,
            NEW.symbol,NEW.cell,NEW.strategy,NEW.amount_usdt,NEW.operator_id,NEW.issued_at_ms)
          IS DISTINCT FROM (OLD.permit_id,OLD.halt_id,OLD.exchange_account_id,OLD.deployment_environment,
            OLD.symbol,OLD.cell,OLD.strategy,OLD.amount_usdt,OLD.operator_id,OLD.issued_at_ms)
          OR (OLD.state='consumed' AND NEW.state <> 'consumed')
          OR (OLD.consumed_at_ms IS NOT NULL AND NEW.consumed_at_ms IS DISTINCT FROM OLD.consumed_at_ms)
          OR (OLD.execution_decision_id IS NOT NULL AND NEW.execution_decision_id IS DISTINCT FROM OLD.execution_decision_id)
          OR (OLD.attempt_id IS NOT NULL AND NEW.attempt_id IS DISTINCT FROM OLD.attempt_id)
          THEN RAISE EXCEPTION 'immutable release permit'; END IF;
        RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER release_permit_binding BEFORE UPDATE ON public.canary_command_permits FOR EACH ROW EXECUTE FUNCTION public.guard_release_permit()")
    op.execute("CREATE TRIGGER release_permit_history BEFORE DELETE ON public.canary_command_permits FOR EACH ROW EXECUTE FUNCTION public.reject_release_mutation()")
    op.execute("CREATE TRIGGER release_permit_no_truncate BEFORE TRUNCATE ON public.canary_command_permits FOR EACH STATEMENT EXECUTE FUNCTION public.reject_release_mutation()")
    for table in ("release_sessions", "release_session_audit"):
        op.execute(f"CREATE TRIGGER release_no_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.reject_release_mutation()")
    # Narrow boolean capability avoids granting runtime access to password/TOTP
    # material. SHARE locks keep authority stable through the promotion commit.
    op.execute('''CREATE FUNCTION public.release_operator_authorized(account uuid, actor text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT EXISTS (SELECT 1 FROM auth."user" u
          JOIN public.exchange_account_memberships m ON m.user_id=u.id
          JOIN public.exchange_accounts a ON a.id=m.exchange_account_id
          WHERE a.id=account AND u.id=actor AND u.role='admin' AND u.banned IS NOT TRUE
            AND u."twoFactorEnabled" IS TRUE AND m.role IN ('owner','operator')
            AND a.lifecycle_status IN ('active','halted') FOR SHARE OF u,m,a)
        $$''')
    op.execute("REVOKE ALL ON FUNCTION public.guard_release_session(), public.guard_release_permit(), public.reject_release_mutation(), public.release_operator_authorized(uuid,text) FROM PUBLIC")
    op.execute("REVOKE ALL ON public.release_sessions, public.release_session_audit FROM PUBLIC")
    request_columns = "id,exchange_account_id,deployment_environment,symbol,cell,strategy,max_amount,expires_at_ms,created_at_ms,requested_by,requested_action,request_revision"
    worker_columns = "processed_revision,state,binding,halt_id,minimum_amount,authorized_by,authorized_at_ms,consumed_at_ms,permit_id,decision_id,attempt_id,exact_amount,evidence,promoted_halt_id,reason"
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.release_sessions,public.release_session_audit FROM bfx_webapi;
        GRANT SELECT ON public.release_sessions,public.release_session_audit TO bfx_webapi;
        GRANT INSERT ({request_columns}) ON public.release_sessions TO bfx_webapi;
        GRANT UPDATE (requested_by,requested_action,request_revision) ON public.release_sessions TO bfx_webapi;
        GRANT INSERT ON public.release_session_audit TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.release_sessions,public.release_session_audit FROM bfx_bot;
        GRANT SELECT ON public.release_sessions,public.release_session_audit TO bfx_bot;
        GRANT UPDATE ({worker_columns}) ON public.release_sessions TO bfx_bot;
        GRANT INSERT ON public.release_session_audit TO bfx_bot;
        GRANT SELECT,INSERT,UPDATE ON public.canary_command_permits TO bfx_bot;
        GRANT SELECT,INSERT ON public.trading_halt TO bfx_bot;
        GRANT USAGE,SELECT ON SEQUENCE public.trading_halt_id_seq TO bfx_bot;
        GRANT EXECUTE ON FUNCTION public.release_operator_authorized(uuid,text) TO bfx_bot;
      END IF; END $$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM public.release_sessions)
        OR EXISTS (SELECT FROM public.execution_decisions WHERE strategy IS NOT NULL) THEN
        RAISE EXCEPTION 'refuse downgrade of populated release evidence'; END IF; END $$""")
    op.drop_table("release_session_audit")
    op.drop_table("release_sessions")
    op.drop_column("execution_decisions", "strategy")
    for trigger in ("release_permit_binding", "release_permit_history", "release_permit_no_truncate"):
        op.execute(f"DROP TRIGGER {trigger} ON public.canary_command_permits")
    op.execute("DROP FUNCTION public.guard_release_permit()")
    op.execute("DROP FUNCTION public.guard_release_session(),public.reject_release_mutation(),public.release_operator_authorized(uuid,text)")
