"""Two-state trading control: retire REDUCING, probation and build approvals.

Revises 1f6392809120. Lending envelope ADR 2026-09-25 D4/D5: the trading state
is ACTIVE or HALTED, caused by ``operator`` or ``auto``; the everyday stop is
the CapitalPolicy ``enabled`` flag; releases no longer need approval and no
resume starts a probation.

- ``trading_state`` keeps its rows (the cancel-all audit and the requests point
  at them). Its state/cause CHECKs are replaced NOT VALID, so rows recorded
  before this revision keep saying REDUCING or ``material_deploy`` while every
  new row is ACTIVE/HALTED by operator/auto. The probation columns are copied
  to ``release_archive.trading_state_probation`` and dropped, with the probation
  trigger. ``guard_trading_state_transition`` now only keeps a halt from being
  ended by anything but an operator (a legacy REDUCING counts as stopped). A
  scope whose current row is REDUCING gets an operator HALTED appended: the
  pause becomes the stricter stop, and only a resume ends it.
- ``deployment_approvals`` and the old ``trading_control_requests`` (approve,
  resume with a build digest, pause, kill) move to ``release_archive`` with a
  manifest row each and the archive's freeze trigger; the old request guard
  function goes with them. A new ``trading_control_requests`` takes ``resume``
  and ``kill`` only and names no build.

Grants follow 1c435a35dcb4: the bot reads requests and records outcomes, the
web API reads them and inserts only the request columns.

Downgrade restores the previous shape: the archived tables and request guard
move back, the probation columns are refilled from the archive copy, and the
old CHECKs and triggers return. Rows written since are kept (every ACTIVE or
HALTED by operator/auto is valid under the old rules too), except that a
populated new request table is refused: its resumes name no build, which the
old digest CHECK requires.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5b1e7c9d2a40"
down_revision = "1f6392809120"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

SCHEMA = "release_archive"
REQUEST_COLUMNS = "request_id,exchange_account_id,deployment_environment,action,reason,requested_by,created_at_ms"
WORKER_COLUMNS = "state,processed_at_ms,outcome_reason,trading_state_id"
_APP_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")

_TRANSITION_GUARD = """CREATE OR REPLACE FUNCTION public.guard_trading_state_transition()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
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
      IF FOUND AND prev.state <> 'ACTIVE' AND NEW.state = 'ACTIVE' AND NEW.cause <> 'operator' THEN
        RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by %', prev.state, NEW.cause;
      END IF;
      RETURN NEW;
    END $$"""


def _digest(table: str, key: str) -> str:
    return (f"SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text, "
            f"E'\\n' ORDER BY t.{key}), ''), 'UTF8')), 'hex') FROM {SCHEMA}.{table} t")


def _archive(table: str, key: str) -> None:
    # The manifest is frozen like every archive table; it opens for exactly
    # this one appended row.
    op.execute(f"ALTER TABLE {SCHEMA}.manifest DISABLE TRIGGER archive_frozen")
    op.execute(f"""INSERT INTO {SCHEMA}.manifest (table_name, row_count, content_sha256,
        archived_by_revision) SELECT '{table}', d.count, d.encode, '{revision}'
        FROM ({_digest(table, key)}) AS d""")
    op.execute(f"ALTER TABLE {SCHEMA}.manifest ENABLE TRIGGER archive_frozen")
    op.execute(f"CREATE TRIGGER archive_frozen BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE "
               f"ON {SCHEMA}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()")
    op.execute(f"""DO $$ DECLARE r text; BEGIN
      FOREACH r IN ARRAY ARRAY{list(_APP_ROLES)} LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
          EXECUTE format('REVOKE ALL ON {SCHEMA}.{table} FROM %I', r);
        END IF;
      END LOOP; END $$""")


def _retire_probation() -> None:
    # The archive copy is taken before the manifest digest, from the rows as
    # recorded; the freeze trigger then keeps it that way.
    op.execute(f"""CREATE TABLE {SCHEMA}.trading_state_probation AS
        SELECT id AS trading_state_id, probation_multiplier, probation_started_at_ms,
               probation_floor
        FROM public.trading_state WHERE probation_multiplier IS NOT NULL""")
    op.execute(f"ALTER TABLE {SCHEMA}.trading_state_probation ADD PRIMARY KEY (trading_state_id)")
    _archive("trading_state_probation", "trading_state_id")
    op.execute("DROP TRIGGER trading_state_transition_probation ON public.trading_state")
    op.execute("DROP FUNCTION public.guard_trading_state_probation()")
    op.drop_constraint("ck_trading_state_probation", "trading_state", type_="check")
    for column in ("probation_multiplier", "probation_started_at_ms", "probation_floor"):
        op.drop_column("trading_state", column)


def _two_states() -> None:
    op.drop_constraint("ck_trading_state_state", "trading_state", type_="check")
    op.drop_constraint("ck_trading_state_cause", "trading_state", type_="check")
    op.execute("ALTER TABLE public.trading_state ADD CONSTRAINT ck_trading_state_state "
               "CHECK (state IN ('ACTIVE', 'HALTED')) NOT VALID")
    op.execute("ALTER TABLE public.trading_state ADD CONSTRAINT ck_trading_state_cause "
               "CHECK (cause IN ('operator', 'auto')) NOT VALID")
    op.execute(_TRANSITION_GUARD)
    op.execute("""INSERT INTO public.trading_state
          (exchange_account_id, deployment_environment, state, cause, actor, reason, created_at_ms)
        SELECT s.exchange_account_id, s.deployment_environment, 'HALTED', 'operator',
               'migration 5b1e7c9d2a40',
               'REDUCING retired (lending envelope ADR); carried over from trading_state '
                 || s.id || ': ' || s.reason,
               (extract(epoch FROM clock_timestamp()) * 1000)::bigint
        FROM (SELECT DISTINCT ON (exchange_account_id, deployment_environment) *
              FROM public.trading_state
              ORDER BY exchange_account_id, deployment_environment, id DESC) AS s
        WHERE s.state = 'REDUCING'
        ORDER BY s.id""")


def _replace_requests() -> None:
    op.execute(f"ALTER TABLE public.deployment_approvals SET SCHEMA {SCHEMA}")
    _archive("deployment_approvals", "id")
    op.execute("ALTER TABLE public.trading_control_requests RENAME TO trading_control_requests_v1")
    op.execute(f"ALTER TABLE public.trading_control_requests_v1 SET SCHEMA {SCHEMA}")
    op.execute(f"ALTER FUNCTION public.guard_trading_control_request() SET SCHEMA {SCHEMA}")
    _archive("trading_control_requests_v1", "request_id")

    op.create_table(
        "trading_control_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_trading_control_requests_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("trading_state_id", sa.BigInteger(),
                  sa.ForeignKey("trading_state.id", ondelete="RESTRICT",
                                name="fk_trading_control_requests_trading_state"), nullable=True),
        sa.CheckConstraint("action IN ('resume', 'kill')", name="ck_trading_control_requests_action"),
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
    op.create_index("uq_trading_control_requests_pending", "trading_control_requests",
                    ["exchange_account_id", "deployment_environment"], unique=True,
                    postgresql_where=sa.text("state = 'requested' AND action <> 'kill'"))
    op.create_index("uq_trading_control_requests_pending_kill", "trading_control_requests",
                    ["exchange_account_id", "deployment_environment"], unique=True,
                    postgresql_where=sa.text("state = 'requested' AND action = 'kill'"))
    op.create_index("ix_trading_control_requests_queue", "trading_control_requests",
                    ["exchange_account_id", "deployment_environment", "state", "created_at_ms"])
    names = REQUEST_COLUMNS.split(",")
    op.execute(f"""CREATE FUNCTION public.guard_trading_control_request() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
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
    op.execute("REVOKE ALL ON FUNCTION public.guard_trading_control_request() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.trading_control_requests FROM PUBLIC")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.trading_control_requests FROM bfx_bot;
        GRANT SELECT ON public.trading_control_requests TO bfx_bot;
        GRANT UPDATE ({WORKER_COLUMNS}) ON public.trading_control_requests TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.trading_control_requests FROM bfx_webapi;
        GRANT SELECT ON public.trading_control_requests TO bfx_webapi;
        GRANT INSERT ({REQUEST_COLUMNS}) ON public.trading_control_requests TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.trading_control_requests FROM bfx_webauth;
      END IF; END $$""")


def upgrade() -> None:
    _retire_probation()
    _two_states()
    _replace_requests()


_OLD_TRANSITION_GUARD = """CREATE OR REPLACE FUNCTION public.guard_trading_state_transition()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.trading_state%ROWTYPE;
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'bfx-trading-state:' || NEW.exchange_account_id::text || ':' || NEW.deployment_environment, 0));
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

_OLD_PROBATION_GUARD = """CREATE FUNCTION public.guard_trading_state_probation() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE started bigint; prev public.trading_state%ROWTYPE;
    BEGIN
      IF NEW.state <> 'ACTIVE' OR NEW.probation_multiplier IS NOT NULL THEN
        RETURN NEW;
      END IF;
      SELECT max(id) INTO started FROM public.trading_state
        WHERE exchange_account_id = NEW.exchange_account_id
          AND deployment_environment = NEW.deployment_environment
          AND probation_multiplier IS NOT NULL;
      IF started IS NULL OR EXISTS (SELECT 1 FROM public.trading_state
          WHERE exchange_account_id = NEW.exchange_account_id
            AND deployment_environment = NEW.deployment_environment
            AND id > started AND state = 'ACTIVE' AND probation_multiplier IS NULL) THEN
        RETURN NEW;
      END IF;
      SELECT * INTO prev FROM public.trading_state
        WHERE exchange_account_id = NEW.exchange_account_id
          AND deployment_environment = NEW.deployment_environment
        ORDER BY id DESC LIMIT 1;
      IF prev.state = 'ACTIVE' AND prev.probation_multiplier IS NOT NULL AND NEW.cause = 'auto' THEN
        RETURN NEW;  -- the lift
      END IF;
      RAISE EXCEPTION 'illegal trading state transition: probation % has not passed', started;
    END $$"""


def _unarchive(table: str) -> None:
    op.execute(f"DROP TRIGGER archive_frozen ON {SCHEMA}.{table}")
    op.execute(f"ALTER TABLE {SCHEMA}.manifest DISABLE TRIGGER archive_frozen")
    op.execute(f"DELETE FROM {SCHEMA}.manifest WHERE table_name = '{table}'")
    op.execute(f"ALTER TABLE {SCHEMA}.manifest ENABLE TRIGGER archive_frozen")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.trading_control_requests) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded resume/kill requests';
        END IF; END $$""")
    op.drop_table("trading_control_requests")
    op.execute("DROP FUNCTION public.guard_trading_control_request()")
    _unarchive("trading_control_requests_v1")
    op.execute(f"ALTER FUNCTION {SCHEMA}.guard_trading_control_request() SET SCHEMA public")
    op.execute(f"ALTER TABLE {SCHEMA}.trading_control_requests_v1 SET SCHEMA public")
    op.execute("ALTER TABLE public.trading_control_requests_v1 RENAME TO trading_control_requests")
    _unarchive("deployment_approvals")
    op.execute(f"ALTER TABLE {SCHEMA}.deployment_approvals SET SCHEMA public")
    # The grants 1c435a35dcb4 gave, which archiving took back.
    old_request_columns = REQUEST_COLUMNS.replace("action,", "action,backend_digest,")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        GRANT SELECT, INSERT ON public.deployment_approvals TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE public.deployment_approvals_id_seq TO bfx_bot;
        GRANT SELECT ON public.trading_control_requests TO bfx_bot;
        GRANT UPDATE ({WORKER_COLUMNS}) ON public.trading_control_requests TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        GRANT SELECT ON public.deployment_approvals, public.trading_control_requests TO bfx_webapi;
        GRANT INSERT ({old_request_columns}) ON public.trading_control_requests TO bfx_webapi;
      END IF; END $$""")

    op.execute(_OLD_TRANSITION_GUARD)
    op.drop_constraint("ck_trading_state_state", "trading_state", type_="check")
    op.drop_constraint("ck_trading_state_cause", "trading_state", type_="check")
    op.create_check_constraint("ck_trading_state_state", "trading_state",
                               "state IN ('ACTIVE', 'REDUCING', 'HALTED')")
    op.create_check_constraint(
        "ck_trading_state_cause", "trading_state",
        "(state = 'ACTIVE' AND cause IN ('operator', 'auto')) OR "
        "(state = 'REDUCING' AND cause IN ('operator', 'material_deploy')) OR "
        "(state = 'HALTED' AND cause IN ('operator', 'auto'))")
    op.add_column("trading_state", sa.Column("probation_multiplier", sa.Numeric(), nullable=True))
    op.add_column("trading_state", sa.Column("probation_started_at_ms", sa.BigInteger(), nullable=True))
    op.add_column("trading_state", sa.Column("probation_floor", postgresql.JSONB(), nullable=True))
    # The one rewrite this history ever takes: putting back what the upgrade moved.
    op.execute("ALTER TABLE public.trading_state DISABLE TRIGGER trading_state_history")
    op.execute(f"""UPDATE public.trading_state t SET probation_multiplier = p.probation_multiplier,
          probation_started_at_ms = p.probation_started_at_ms, probation_floor = p.probation_floor
        FROM {SCHEMA}.trading_state_probation p WHERE p.trading_state_id = t.id""")
    op.execute("ALTER TABLE public.trading_state ENABLE TRIGGER trading_state_history")
    op.create_check_constraint(
        "ck_trading_state_probation", "trading_state",
        "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL "
        "AND probation_floor IS NULL) OR "
        "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
        "AND probation_started_at_ms >= 0 AND probation_floor IS NOT NULL)")
    op.execute(_OLD_PROBATION_GUARD)
    op.execute("REVOKE ALL ON FUNCTION public.guard_trading_state_probation() FROM PUBLIC")
    op.execute("CREATE TRIGGER trading_state_transition_probation BEFORE INSERT ON public.trading_state "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_trading_state_probation()")
    _unarchive("trading_state_probation")
    op.execute(f"DROP TABLE {SCHEMA}.trading_state_probation")
