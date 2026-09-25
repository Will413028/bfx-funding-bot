"""Trading state: ACTIVE / REDUCING / HALTED, append-only and independent of releases.

`trading_halt` carried one boolean plus a `kind` that decided how a halt could be
cleared, and the only way back from most halts was a release promotion. The
trading state replaces it as the authority on whether new offers may be placed
(ADR 2026-09-25 D4): a maintenance pause is REDUCING (cancels only), a kill is
HALTED, and each transition names its cause. `trading_halt` is kept, unread by
trading decisions, until the release ceremony that still binds its epochs to it
is removed.

Rows are never updated or deleted; the current state is the highest id for the
account/environment. The insert trigger serialises writers per scope and assigns
the id after taking that lock, so id order is decision order even for a raw SQL
writer, then rejects the transitions that would weaken a stop:

- HALTED never becomes REDUCING (a halt ends only in an operator's resume);
- nothing leaves REDUCING or HALTED for ACTIVE except an operator;
- a material-deploy REDUCING cannot be relabelled as an operator pause, which
  would let the cheaper resume skip the approval the deploy is waiting for.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8e4f33517b10"
down_revision = "a7f3c1d9e204"
branch_labels = None
depends_on = None

_GUARD = """CREATE FUNCTION public.guard_trading_state_transition() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.trading_state%ROWTYPE;
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'bfx-trading-state:' || NEW.exchange_account_id::text || ':' || NEW.deployment_environment, 0));
      -- Assigned under the scope lock: a row that waited here must not keep an
      -- id drawn before the row it waited for, or "highest id" would not be
      -- the latest decision.
      NEW.id := nextval('public.trading_state_id_seq');
      SELECT * INTO prev FROM public.trading_state
        WHERE exchange_account_id = NEW.exchange_account_id
          AND deployment_environment = NEW.deployment_environment
        ORDER BY id DESC LIMIT 1;
      IF FOUND THEN
        IF prev.state = 'HALTED' AND NEW.state = 'REDUCING' THEN
          RAISE EXCEPTION 'illegal trading state transition HALTED -> REDUCING';
        END IF;
        IF prev.state <> 'ACTIVE' AND NEW.state = 'ACTIVE' AND NEW.cause <> 'operator' THEN
          RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by %', prev.state, NEW.cause;
        END IF;
        IF prev.state = 'REDUCING' AND prev.cause = 'material_deploy'
           AND NEW.state = 'REDUCING' AND NEW.cause <> 'material_deploy' THEN
          RAISE EXCEPTION 'illegal trading state transition: material deploy approval cannot be relabelled';
        END IF;
      END IF;
      RETURN NEW;
    END $$"""


def upgrade() -> None:
    op.create_table(
        "trading_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "exchange_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                          name="fk_trading_state_account"),
            nullable=False,
        ),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        # Reserved for the probation period that follows an approval (T5).
        sa.Column("probation_multiplier", sa.Numeric(), nullable=True),
        sa.Column("probation_started_at_ms", sa.BigInteger(), nullable=True),
        # Provenance of the rows this migration carries over from trading_halt.
        sa.Column("legacy_halt_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state IN ('ACTIVE', 'REDUCING', 'HALTED')", name="ck_trading_state_state"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND cause IN ('operator', 'auto')) OR "
            "(state = 'REDUCING' AND cause IN ('operator', 'material_deploy')) OR "
            "(state = 'HALTED' AND cause IN ('operator', 'kill_switch', 'auto'))",
            name="ck_trading_state_cause",
        ),
        sa.CheckConstraint(
            "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL) OR "
            "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
            "AND probation_started_at_ms >= 0)",
            name="ck_trading_state_probation",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) > 0 AND length(trim(reason)) > 0 "
            "AND length(trim(deployment_environment)) > 0 AND created_at_ms >= 0",
            name="ck_trading_state_evidence",
        ),
    )
    op.create_index(
        "ix_trading_state_scope_id", "trading_state",
        ["exchange_account_id", "deployment_environment", "id"],
    )
    op.execute(_GUARD)
    op.execute("""CREATE FUNCTION public.reject_trading_state_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'immutable trading state history'; END $$""")
    op.execute("CREATE TRIGGER trading_state_transition BEFORE INSERT ON public.trading_state "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_trading_state_transition()")
    op.execute("CREATE TRIGGER trading_state_history BEFORE UPDATE OR DELETE ON public.trading_state "
               "FOR EACH ROW EXECUTE FUNCTION public.reject_trading_state_mutation()")
    op.execute("CREATE TRIGGER trading_state_no_truncate BEFORE TRUNCATE ON public.trading_state "
               "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_trading_state_mutation()")

    # Carry each scope's current halt decision over, verbatim. A maintenance
    # pause becomes REDUCING; every other halt -- safety or release, whoever
    # wrote it -- becomes an operator HALTED, the strictest state, because
    # nothing here can prove a weaker one. A cleared halt is ACTIVE.
    op.execute("""INSERT INTO public.trading_state
          (exchange_account_id, deployment_environment, state, cause, actor, reason,
           created_at_ms, legacy_halt_id)
        SELECT h.exchange_account_id, h.deployment_environment,
               CASE WHEN NOT h.halted THEN 'ACTIVE'
                    WHEN h.kind = 'maintenance' THEN 'REDUCING'
                    ELSE 'HALTED' END,
               'operator',
               CASE WHEN btrim(h.actor) = '' THEN 'legacy' ELSE h.actor END,
               CASE WHEN btrim(h.reason) = '' THEN 'trading_halt ' || h.id || ' recorded no reason'
                    ELSE h.reason END,
               GREATEST(h.created_at_ms, 0), h.id
        FROM (SELECT DISTINCT ON (exchange_account_id, deployment_environment) *
              FROM public.trading_halt WHERE exchange_account_id IS NOT NULL
              ORDER BY exchange_account_id, deployment_environment, id DESC) AS h
        ORDER BY h.id""")

    op.execute("REVOKE ALL ON FUNCTION public.guard_trading_state_transition(), "
               "public.reject_trading_state_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.trading_state FROM PUBLIC")
    op.execute("REVOKE ALL ON SEQUENCE public.trading_state_id_seq FROM PUBLIC")
    # The daemon appends decisions; the web API only reads them (ADR D4': the
    # web API holds no write on execution state). Default privileges may have
    # granted either role more, so both are reset before the grant.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.trading_state FROM bfx_bot;
        REVOKE ALL ON SEQUENCE public.trading_state_id_seq FROM bfx_bot;
        GRANT SELECT, INSERT ON public.trading_state TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE public.trading_state_id_seq TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.trading_state FROM bfx_webapi;
        REVOKE ALL ON SEQUENCE public.trading_state_id_seq FROM bfx_webapi;
        GRANT SELECT ON public.trading_state TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.trading_state FROM bfx_webauth;
        REVOKE ALL ON SEQUENCE public.trading_state_id_seq FROM bfx_webauth;
      END IF;
    END $$""")


def downgrade() -> None:
    # Rows carried over from trading_halt can be dropped: that table still holds
    # them. A decision recorded after this migration exists nowhere else.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.trading_state WHERE legacy_halt_id IS NULL) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded trading state decisions';
        END IF; END $$""")
    op.drop_index("ix_trading_state_scope_id", table_name="trading_state")
    op.drop_table("trading_state")
    op.execute("DROP FUNCTION public.guard_trading_state_transition(), "
               "public.reject_trading_state_mutation()")
