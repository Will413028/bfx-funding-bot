"""Trading governance: the trading state, its stops and the operator outboxes.

Revises a7f3c1d9e204 (production). ADR 2026-09-25 D2/D3/D4/D4'. Creates:

- ``trading_state`` -- ACTIVE / REDUCING / HALTED, append-only, the authority on
  whether new offers may be placed. Each row names its cause: ACTIVE by
  ``operator`` or ``auto``; REDUCING by ``operator`` (a pause) or
  ``material_deploy``; HALTED by ``operator`` or ``auto``. The current state is
  the highest id for the account/environment. ``guard_trading_state_transition``
  serialises writers per scope, assigns the id under that lock (so id order is
  decision order even for a raw SQL writer) and rejects the transitions that
  would weaken a stop: HALTED never becomes REDUCING; nothing leaves REDUCING
  or HALTED for ACTIVE except an operator; a material-deploy REDUCING cannot be
  relabelled as an operator pause. ``guard_trading_state_probation`` (fires
  second, by name) keeps an ACTIVE row inside an unfinished probation: only the
  lift -- ACTIVE by ``auto`` without a probation, from inside one -- ends it; a
  pause or an operator's stop does not. A probation row carries its multiplier,
  start and ``probation_floor`` (per-currency venue minimum, native units, with
  the submit margin). Each scope's current ``trading_halt`` decision is carried
  over (``legacy_halt_id``): a maintenance pause as REDUCING, any other halt as
  an operator HALTED, a cleared halt as ACTIVE.
- ``funding_cancel_all_audit`` -- one ``requested`` row before every venue
  funding cancel-all a stop issues, then exactly one terminal row
  (``acknowledged``, ``rejected``, ``failed``, ``skipped``); a retry is a new
  attempt. Append-only.
- ``deployment_approvals`` -- append-only, one row per approved backend digest.
- ``trading_control_requests`` -- the web API's request to approve, resume,
  pause or kill; the account daemon applies it under the account lock and
  records one outcome. Approve and resume name the build the operator saw; a
  stop never names one. One request may wait per scope, plus one kill beside it.
- ``operator_authorized(account, actor)`` -- the operator check the daemon
  re-runs when it applies any operator request.
- ``uncertainty_resolution_requests`` -- the operator adjudication outbox. The
  web API loses every ledger and execution write (including the grants added
  by hand on 2026-09-22); it inserts request rows and the daemon applies them.
- ``nav_window_samples`` -- the loss limiter's 24h NAV window, so a restart
  does not forget a loss. The bot reads, appends and prunes.

Grants: the bot appends to the histories and records request outcomes; the web
API reads them and inserts only the request columns. Default privileges may
have granted either role more, so every grant is preceded by a REVOKE.

Downgrade refuses while any decision, audit row, approval or request recorded
after this revision exists (carried-over halts can go: ``trading_halt`` still
holds them). The web API writes revoked here are not granted back: that is a
decision for whoever downgrades, not a side effect.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "1c435a35dcb4"
down_revision = "a7f3c1d9e204"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_DIGEST = "'^sha256:[0-9a-f]{64}$'"
REQUEST_STATES = ("requested", "applied", "rejected", "failed")

TRADING_CONTROL_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,action,backend_digest,"
    "reason,requested_by,created_at_ms"
)
TRADING_CONTROL_WORKER_COLUMNS = "state,processed_at_ms,outcome_reason,trading_state_id"

UNCERTAINTY_ACTIONS = ("bind_to_venue", "mark_not_accepted", "manual_resolution")
UNCERTAINTY_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "reconcile_event_seq,venue_offer_id,decision,reason,requested_by,created_at_ms"
)
UNCERTAINTY_WORKER_COLUMNS = "state,processed_at_ms,resolved_event_seq,outcome_reason"
# The ledger, its projections and the other execution tables: the web API
# writes none of them.
_NO_WEBAPI_WRITE = (
    "event_log", "event_prefix_hashes", "projection_heads",
    "execution_uncertainties", "offer_claims", "position_state", "reconcile_observation",
    "submission_attempts", "venue_credit_state", "venue_offer_state",
    "capital_snapshots", "capital_snapshot_queries", "capital_policy_revisions",
    "capital_policy_heads", "execution_decisions", "canary_command_permits", "trading_halt",
)
# Hand-granted on 2026-09-22 for the synchronous append; not part of the web
# API's read baseline.
_NO_WEBAPI_ACCESS = ("event_prefix_hashes", "projection_heads")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_TRANSITION_GUARD = """CREATE FUNCTION public.guard_trading_state_transition() RETURNS trigger
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

_PROBATION_GUARD = """CREATE FUNCTION public.guard_trading_state_probation() RETURNS trigger
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


def _reject_function(name: str, message: str) -> None:
    op.execute(f"""CREATE FUNCTION public.{name}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION '{message}'; END $$""")


def _append_only(table: str, function: str, *, row_ops: str = "UPDATE OR DELETE",
                 row_trigger: str, truncate_trigger: str) -> None:
    op.execute(f"CREATE TRIGGER {row_trigger} BEFORE {row_ops} ON public.{table} "
               f"FOR EACH ROW EXECUTE FUNCTION public.{function}()")
    op.execute(f"CREATE TRIGGER {truncate_trigger} BEFORE TRUNCATE ON public.{table} "
               f"FOR EACH STATEMENT EXECUTE FUNCTION public.{function}()")


def _request_guard(name: str, columns: str, immutable: str, transition: str) -> None:
    """What the operator asked for is fixed once written; the daemon records one
    terminal outcome and nothing rewrites it."""
    names = columns.split(",")
    op.execute(f"""CREATE FUNCTION public.{name}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
          THEN RAISE EXCEPTION '{immutable}'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION '{transition}'; END IF;
        RETURN NEW; END $$""")


def _account_fk(table: str) -> sa.Column:
    return sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                     sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                   name=f"fk_{table}_account"), nullable=False)


def _create_trading_state() -> None:
    op.create_table(
        "trading_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _account_fk("trading_state"),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("probation_multiplier", sa.Numeric(), nullable=True),
        sa.Column("probation_started_at_ms", sa.BigInteger(), nullable=True),
        # Provenance of the rows carried over from trading_halt (no foreign key:
        # the halt table is archived by the next revision).
        sa.Column("legacy_halt_id", sa.BigInteger(), nullable=True),
        sa.Column("probation_floor", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("state IN ('ACTIVE', 'REDUCING', 'HALTED')", name="ck_trading_state_state"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND cause IN ('operator', 'auto')) OR "
            "(state = 'REDUCING' AND cause IN ('operator', 'material_deploy')) OR "
            "(state = 'HALTED' AND cause IN ('operator', 'auto'))",
            name="ck_trading_state_cause",
        ),
        sa.CheckConstraint(
            "(probation_multiplier IS NULL AND probation_started_at_ms IS NULL "
            "AND probation_floor IS NULL) OR "
            "(state = 'ACTIVE' AND probation_multiplier > 0 AND probation_multiplier <= 1 "
            "AND probation_started_at_ms >= 0 AND probation_floor IS NOT NULL)",
            name="ck_trading_state_probation",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) > 0 AND length(trim(reason)) > 0 "
            "AND length(trim(deployment_environment)) > 0 AND created_at_ms >= 0",
            name="ck_trading_state_evidence",
        ),
    )
    op.create_index("ix_trading_state_scope_id", "trading_state",
                    ["exchange_account_id", "deployment_environment", "id"])
    op.execute(_TRANSITION_GUARD)
    op.execute(_PROBATION_GUARD)
    _reject_function("reject_trading_state_mutation", "immutable trading state history")
    op.execute("CREATE TRIGGER trading_state_transition BEFORE INSERT ON public.trading_state "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_trading_state_transition()")
    # Named after trading_state_transition so it fires second (same scope lock).
    op.execute("CREATE TRIGGER trading_state_transition_probation BEFORE INSERT ON public.trading_state "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_trading_state_probation()")
    _append_only("trading_state", "reject_trading_state_mutation",
                 row_trigger="trading_state_history", truncate_trigger="trading_state_no_truncate")

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


def _create_cancel_all_audit() -> None:
    op.create_table(
        "funding_cancel_all_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _account_fk("funding_cancel_all_audit"),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        # The HALTED decision this cancel-all enforces.
        sa.Column("trading_state_id", sa.BigInteger(),
                  sa.ForeignKey("trading_state.id", ondelete="RESTRICT",
                                name="fk_funding_cancel_all_audit_trading_state"), nullable=False),
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
    op.create_index("ix_funding_cancel_all_audit_scope_id", "funding_cancel_all_audit",
                    ["exchange_account_id", "deployment_environment", "id"])
    # One request and at most one outcome per attempt.
    op.create_index("uq_funding_cancel_all_audit_request", "funding_cancel_all_audit", ["attempt_id"],
                    unique=True, postgresql_where=sa.text("phase = 'requested'"))
    op.create_index("uq_funding_cancel_all_audit_outcome", "funding_cancel_all_audit", ["attempt_id"],
                    unique=True, postgresql_where=sa.text("phase <> 'requested'"))
    _reject_function("reject_funding_cancel_all_audit_mutation", "immutable funding cancel-all audit")
    _append_only("funding_cancel_all_audit", "reject_funding_cancel_all_audit_mutation",
                 row_trigger="funding_cancel_all_audit_history",
                 truncate_trigger="funding_cancel_all_audit_no_truncate")


def _create_trading_control() -> None:
    op.create_table(
        "deployment_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _account_fk("deployment_approvals"),
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
        _account_fk("trading_control_requests"),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("backend_digest", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("trading_state_id", sa.BigInteger(),
                  sa.ForeignKey("trading_state.id", ondelete="RESTRICT",
                                name="fk_trading_control_requests_trading_state"), nullable=True),
        sa.CheckConstraint("action IN ('approve', 'resume', 'pause', 'kill')",
                           name="ck_trading_control_requests_action"),
        sa.CheckConstraint(
            # IS NOT NULL: a NULL digest would make the regex NULL, and a NULL
            # CHECK passes.
            f"(action IN ('approve', 'resume') AND backend_digest IS NOT NULL "
            f"AND backend_digest ~ {_DIGEST}) OR "
            "(action IN ('pause', 'kill') AND backend_digest IS NULL)",
            name="ck_trading_control_requests_digest"),
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
    _reject_function("reject_trading_control_mutation", "immutable trading control history")
    _append_only("deployment_approvals", "reject_trading_control_mutation",
                 row_trigger="deployment_approvals_history",
                 truncate_trigger="deployment_approvals_no_truncate")
    _request_guard("guard_trading_control_request", TRADING_CONTROL_REQUEST_COLUMNS,
                   "immutable trading control request", "invalid trading control request transition")
    op.execute("CREATE TRIGGER trading_control_request_transition BEFORE UPDATE "
               "ON public.trading_control_requests FOR EACH ROW "
               "EXECUTE FUNCTION public.guard_trading_control_request()")
    _append_only("trading_control_requests", "reject_trading_control_mutation", row_ops="DELETE",
                 row_trigger="trading_control_request_no_delete",
                 truncate_trigger="trading_control_request_no_truncate")
    op.execute('''CREATE FUNCTION public.operator_authorized(account uuid, actor text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT EXISTS (SELECT 1 FROM auth."user" u
          JOIN public.exchange_account_memberships m ON m.user_id=u.id
          JOIN public.exchange_accounts a ON a.id=m.exchange_account_id
          WHERE a.id=account AND u.id=actor AND u.role='admin' AND u.banned IS NOT TRUE
            AND u."twoFactorEnabled" IS TRUE AND m.role IN ('owner','operator')
            AND a.lifecycle_status IN ('active','halted') FOR SHARE OF u,m,a)
        $$''')


def _create_uncertainty_requests() -> None:
    op.create_table(
        "uncertainty_resolution_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        _account_fk("uncertainty_resolution_requests"),
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
        sa.Column("resolved_event_seq", sa.BigInteger(),
                  sa.ForeignKey("event_log.event_seq", ondelete="RESTRICT",
                                name="fk_uncertainty_resolution_requests_event"), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(f"action IN ({_in(UNCERTAINTY_ACTIONS)})",
                           name="ck_uncertainty_resolution_requests_action"),
        sa.CheckConstraint(f"state IN ({_in(REQUEST_STATES)})",
                           name="ck_uncertainty_resolution_requests_state"),
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
    op.create_index("uq_uncertainty_resolution_requests_pending", "uncertainty_resolution_requests",
                    ["exchange_account_id", "deployment_environment", "uncertainty_id"], unique=True,
                    postgresql_where=sa.text("state = 'requested'"))
    op.create_index("ix_uncertainty_resolution_requests_queue", "uncertainty_resolution_requests",
                    ["exchange_account_id", "deployment_environment", "state", "created_at_ms"])
    _request_guard("guard_uncertainty_resolution_request", UNCERTAINTY_REQUEST_COLUMNS,
                   "immutable uncertainty resolution request", "invalid uncertainty resolution transition")
    _reject_function("reject_uncertainty_resolution_request_mutation",
                     "immutable uncertainty resolution history")
    op.execute("""CREATE TRIGGER uncertainty_resolution_request_transition
        BEFORE UPDATE ON public.uncertainty_resolution_requests
        FOR EACH ROW EXECUTE FUNCTION public.guard_uncertainty_resolution_request()""")
    _append_only("uncertainty_resolution_requests", "reject_uncertainty_resolution_request_mutation",
                 row_ops="DELETE", row_trigger="uncertainty_resolution_request_no_delete",
                 truncate_trigger="uncertainty_resolution_request_no_truncate")


def _create_nav_window_samples() -> None:
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
    op.create_index("ix_nav_window_samples_scope_symbol_time", "nav_window_samples",
                    ["exchange_account_id", "deployment_environment", "symbol", "occurred_at_ms"])


_FUNCTIONS = (
    "guard_trading_state_transition()", "guard_trading_state_probation()",
    "reject_trading_state_mutation()", "reject_funding_cancel_all_audit_mutation()",
    "reject_trading_control_mutation()", "guard_trading_control_request()",
    "operator_authorized(uuid,text)", "guard_uncertainty_resolution_request()",
    "reject_uncertainty_resolution_request_mutation()",
)
_TABLES = ("trading_state", "funding_cancel_all_audit", "deployment_approvals",
           "trading_control_requests", "uncertainty_resolution_requests", "nav_window_samples")
_SEQUENCES = ("trading_state_id_seq", "funding_cancel_all_audit_id_seq",
              "deployment_approvals_id_seq", "nav_window_samples_id_seq")


def _grant() -> None:
    functions = ", ".join(f"public.{function}" for function in _FUNCTIONS)
    tables = ", ".join(f"public.{table}" for table in _TABLES)
    sequences = ", ".join(f"public.{sequence}" for sequence in _SEQUENCES)
    op.execute(f"REVOKE ALL ON FUNCTION {functions} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {tables} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON SEQUENCE {sequences} FROM PUBLIC")
    no_write = ", ".join(f"'{table}'" for table in _NO_WEBAPI_WRITE)
    no_access = ", ".join(f"'{table}'" for table in _NO_WEBAPI_ACCESS)
    op.execute(f"""DO $$ DECLARE t text; BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON {tables} FROM bfx_bot;
        REVOKE ALL ON SEQUENCE {sequences} FROM bfx_bot;
        GRANT SELECT, INSERT ON public.trading_state, public.funding_cancel_all_audit,
          public.deployment_approvals TO bfx_bot;
        GRANT USAGE, SELECT ON SEQUENCE {sequences} TO bfx_bot;
        GRANT SELECT ON public.trading_control_requests, public.uncertainty_resolution_requests TO bfx_bot;
        GRANT UPDATE ({TRADING_CONTROL_WORKER_COLUMNS}) ON public.trading_control_requests TO bfx_bot;
        GRANT UPDATE ({UNCERTAINTY_WORKER_COLUMNS}) ON public.uncertainty_resolution_requests TO bfx_bot;
        GRANT EXECUTE ON FUNCTION public.operator_authorized(uuid,text) TO bfx_bot;
        GRANT SELECT, INSERT, DELETE ON public.nav_window_samples TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON {tables} FROM bfx_webapi;
        REVOKE ALL ON SEQUENCE {sequences} FROM bfx_webapi;
        GRANT SELECT ON public.trading_state, public.funding_cancel_all_audit,
          public.deployment_approvals, public.trading_control_requests,
          public.uncertainty_resolution_requests TO bfx_webapi;
        GRANT INSERT ({TRADING_CONTROL_REQUEST_COLUMNS}) ON public.trading_control_requests TO bfx_webapi;
        GRANT INSERT ({UNCERTAINTY_REQUEST_COLUMNS})
          ON public.uncertainty_resolution_requests TO bfx_webapi;
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
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON {tables} FROM bfx_webauth;
        REVOKE ALL ON SEQUENCE {sequences} FROM bfx_webauth;
      END IF;
    END $$""")


def upgrade() -> None:
    _create_trading_state()
    _create_cancel_all_audit()
    _create_trading_control()
    _create_uncertainty_requests()
    _create_nav_window_samples()
    _grant()


def downgrade() -> None:
    # Rows carried over from trading_halt can be dropped: that table still holds
    # them. Anything recorded after this revision exists nowhere else.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.trading_state WHERE legacy_halt_id IS NULL) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded trading state decisions';
        END IF;
        IF EXISTS (SELECT FROM public.funding_cancel_all_audit) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded funding cancel-all attempts';
        END IF;
        IF EXISTS (SELECT FROM public.deployment_approvals)
           OR EXISTS (SELECT FROM public.trading_control_requests) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded approvals or trading control requests';
        END IF;
        IF EXISTS (SELECT FROM public.uncertainty_resolution_requests) THEN
          RAISE EXCEPTION 'refuse downgrade of populated uncertainty resolution requests';
        END IF; END $$""")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION " + ", ".join(f"public.{function}" for function in _FUNCTIONS))
