"""Effects of an operator request name it; the request stops naming its effect (R1').

ADR 2026-10-08 operator-requests-keep-state-effects-carry-request-id (D8, D9). The request
row keeps its state columns; every row a request produces points back at it, with the
request's scope in the foreign key, so an effect can only name a request of its own account,
environment and subject, and at most one trading state or revision names a request:

* preconditions (D7'): the existing rows must back-fill cleanly -- no ``requested`` row that
  already has an effect, no rejected or failed uncertainty request a journal row names, no
  applied uncertainty request without its journal row, no settled row without
  ``processed_at_ms``, no effect of another scope or subject, no revision whose
  ``source.request_id`` is not a request of its scope and currency or names one twice, no
  kill window holding two cancel-alls of one currency. Any violation refuses the whole
  revision and names the counts; nothing has changed by then;
* each request table gets the UNIQUE its effects' composite FK references: trading
  ``(request_id, account, environment)``, capital ``+ symbol``, uncertainty
  ``+ uncertainty_id``; ``trading_state`` gets ``(id, account, environment)`` so the
  cancel-all audit's link to the HALTED row it enforces carries the scope too;
* ``trading_state``, ``capital_policy_revisions`` and ``funding_cancel_all_audit`` get
  ``operator_request_id`` (NULL: not caused by a request -- an automatic halt, ``/admin/halt``,
  the owner's amendment script) with the composite FK; the first two a partial UNIQUE (the
  audit holds one attempt per currency, two rows each). ``execution_resolution_journal``
  already has the column and its partial UNIQUE; its FK becomes two MATCH SIMPLE composite
  ones, one through ``attempt_id`` and one through ``quarantine_id``:
  ``ck_execution_resolution_subject`` keeps exactly one of them non-NULL, so exactly one
  checks that the row resolves the uncertainty its request names;
* back-fill as the owner, with each effect table's append-only trigger disabled for the
  UPDATE and enabled again before the postconditions check it is. Capital: the revision's
  ``source.request_id``. Trading: the row an applied request names in ``trading_state_id``
  when it is the operator row that request wrote (cause ``operator``, its actor, reason
  ``'kill: '`` or ``'resumed: '`` and the request's reason); a request that only restated a
  halt names a row it did not write, and of several matches the earliest processed wins.
  Cancel-all: every attempt of the kill's actor against the kill's HALTED row that started
  in the kill's window (its ``processed_at_ms`` up to the scope's next applied kill);
* postconditions, counted independently of the back-fill: a revision's typed column equals
  its ``source.request_id``; matching operator rows equal the trading rows that carry a
  request, and each was written between its request's creation and processing; an attempt's
  rows agree on their request; the three append-only triggers are enabled;
* the request tables' guards drop their list of immutable columns: the runtime roles' column
  grants keep them (asserted here); the transition rule stays (``state`` leaves
  ``requested`` exactly once, the owner included), and so does G2 (an applied uncertainty
  request has its journal row);
* G1 (``guard_runtime_policy_revision``) authorises by the typed ``operator_request_id``;
  ``source.request_id`` is audit text from here on;
* the request tables' product columns (``trading_state_id``, ``policy_revision_id``) close:
  the bot loses their UPDATE grant (asserted) and the outcome CHECKs stop naming them, so the
  next release can drop them without taking the CHECKs along.

Grants: the new columns follow each table's existing table-level grants (the bot reads and
inserts, the web API reads). The journal's web API column grant is unchanged.

Compatibility: the previous web API image selects the request tables' columns, which all
still exist, and inserts none of the effect tables. Forward-only
(docs/adr/2026-10-08-forward-only-migrations.md): the downgrade raises.

Revision ID: 5e820d6dc7da
Revises: 8ac3b44460fc
"""

from sqlalchemy import text

from alembic import op

revision = "5e820d6dc7da"
down_revision = "8ac3b44460fc"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_WRITER = "bfx_bot"
_READER = "bfx_webapi"

# request table -> (the UNIQUE its effects reference, its columns).
REQUEST_SCOPE_KEYS = {
    "trading_control_requests": (
        "uq_trading_control_requests_scope",
        "request_id, exchange_account_id, deployment_environment",
    ),
    "capital_policy_requests": (
        "uq_capital_policy_requests_scope",
        "request_id, exchange_account_id, deployment_environment, symbol",
    ),
    "uncertainty_resolution_requests": (
        "uq_uncertainty_resolution_requests_scope",
        "request_id, exchange_account_id, deployment_environment, uncertainty_id",
    ),
}
# effect table -> (its request table, the FK's columns on the effect, FK name, UNIQUE name).
EFFECT_KEYS = {
    "trading_state": (
        "trading_control_requests",
        "operator_request_id, exchange_account_id, deployment_environment",
        "fk_trading_state_operator_request",
        "uq_trading_state_operator_request",
    ),
    "capital_policy_revisions": (
        "capital_policy_requests",
        "operator_request_id, exchange_account_id, deployment_environment, symbol",
        "fk_capital_policy_revisions_operator_request",
        "uq_capital_policy_revisions_operator_request",
    ),
    "funding_cancel_all_audit": (
        "trading_control_requests",
        "operator_request_id, exchange_account_id, deployment_environment",
        "fk_funding_cancel_all_audit_operator_request",
        None,
    ),
}
# The journal's link to its request, once per subject column (MATCH SIMPLE: the NULL one is
# not checked).
JOURNAL_FKS = {
    "fk_execution_resolution_journal_request_attempt": "attempt_id",
    "fk_execution_resolution_journal_request_quarantine": "quarantine_id",
}
_JOURNAL_LEGACY_FK = "execution_resolution_journal_operator_request_id_fkey"
TRADING_STATE_SCOPE_KEY = "uq_trading_state_scope"
AUDIT_TRADING_STATE_FK = "fk_funding_cancel_all_audit_trading_state"

# effect table -> its append-only row trigger, disabled only for the back-fill.
APPEND_ONLY_TRIGGERS = {
    "trading_state": "trading_state_history",
    "capital_policy_revisions": "immutable_capital_write",
    "funding_cancel_all_audit": "funding_cancel_all_audit_history",
}

# The columns a runtime role must not UPDATE on each request table: what the operator asked.
REQUEST_COLUMNS = {
    "trading_control_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    ),
    "capital_policy_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    ),
    "uncertainty_resolution_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "action", "observation_id", "venue_offer_id", "decision", "reason", "requested_by",
        "created_at_ms",
    ),
}
# request table -> the product column it stops writing (dropped by the next release).
CLOSED_COLUMNS = {
    "trading_control_requests": "trading_state_id",
    "capital_policy_requests": "policy_revision_id",
}
# The outcome CHECKs without the product columns: the state-shape rule each already had.
OUTCOME_CHECKS = {
    "trading_control_requests": "ck_trading_control_requests_outcome",
    "capital_policy_requests": "ck_capital_policy_requests_outcome",
}
OUTCOME_SHAPE = (
    "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
    "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
    "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
    "AND outcome_reason IS NOT NULL)"
)

# The operator row an applied trading request wrote: its trading_state_id names it, and the
# worker wrote cause, actor and reason from the request (modules/execution/trading_control.py).
# processed_at_ms is not part of the key: it and created_at_ms are two clock reads.
_TRADING_MATCH = (
    "FROM public.trading_control_requests r JOIN public.trading_state s "
    "ON s.id = r.trading_state_id AND s.cause = 'operator' AND s.actor = r.requested_by "
    "AND s.reason = (CASE r.action WHEN 'kill' THEN 'kill: ' ELSE 'resumed: ' END) || r.reason "
    "WHERE r.state = 'applied'"
)

# Each applied kill's window: from its processed_at_ms to the next applied kill of the scope.
# The kill switch runs after the kill commits, before the worker takes the next request, and
# restates the same HALTED row with one attempt per currency
# (modules/execution/safety/kill_switch.py), so the kill's attempts are those of its actor
# against its HALTED row that started inside the window.
_KILL_ATTEMPTS = """
    WITH kills AS (
      SELECT r.request_id, r.exchange_account_id, r.deployment_environment, r.requested_by,
             r.trading_state_id, r.processed_at_ms AS opened,
             lead(r.processed_at_ms) OVER (
               PARTITION BY r.exchange_account_id, r.deployment_environment
               ORDER BY r.processed_at_ms, r.request_id) AS closed
      FROM public.trading_control_requests r
      WHERE r.state = 'applied' AND r.action = 'kill'),
    attempts AS (
      SELECT a.attempt_id, a.exchange_account_id, a.deployment_environment, a.trading_state_id,
             a.actor, a.currency, min(a.occurred_at_ms) AS started
      FROM public.funding_cancel_all_audit a
      GROUP BY a.attempt_id, a.exchange_account_id, a.deployment_environment,
               a.trading_state_id, a.actor, a.currency)
    SELECT k.request_id, t.attempt_id, t.currency
    FROM kills k JOIN attempts t
      ON t.exchange_account_id = k.exchange_account_id
     AND t.deployment_environment = k.deployment_environment
     AND t.actor = k.requested_by AND t.trading_state_id = k.trading_state_id
     AND t.started >= k.opened AND (k.closed IS NULL OR t.started < k.closed)"""

_UUID_TEXT = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"

# D7' preconditions: (what is wrong, the count of rows showing it). Each must be 0.
PRECONDITIONS: tuple[tuple[str, str], ...] = (
    ("trading request still requested but linked to a trading state",
     "SELECT count(*) FROM trading_control_requests "
     "WHERE state = 'requested' AND trading_state_id IS NOT NULL"),
    ("capital request still requested but linked to a revision",
     "SELECT count(*) FROM capital_policy_requests "
     "WHERE state = 'requested' AND policy_revision_id IS NOT NULL"),
    ("capital request still requested but a revision names it",
     "SELECT count(*) FROM capital_policy_requests r WHERE r.state = 'requested' AND EXISTS ("
     "SELECT FROM capital_policy_revisions v WHERE v.source ->> 'request_id' = r.request_id::text)"),
    ("uncertainty request still requested but a journal row names it",
     "SELECT count(*) FROM uncertainty_resolution_requests r WHERE r.state = 'requested' AND EXISTS ("
     "SELECT FROM execution_resolution_journal j WHERE j.operator_request_id = r.request_id)"),
    ("uncertainty request rejected or failed but a journal row names it",
     "SELECT count(*) FROM uncertainty_resolution_requests r "
     "WHERE r.state IN ('rejected', 'failed') AND EXISTS ("
     "SELECT FROM execution_resolution_journal j WHERE j.operator_request_id = r.request_id)"),
    ("uncertainty request applied without a journal row",
     "SELECT count(*) FROM uncertainty_resolution_requests r WHERE r.state = 'applied' "
     "AND NOT EXISTS (SELECT FROM execution_resolution_journal j "
     "WHERE j.operator_request_id = r.request_id)"),
    ("settled request without processed_at_ms",
     "SELECT (SELECT count(*) FROM trading_control_requests "
     "WHERE state <> 'requested' AND processed_at_ms IS NULL) + "
     "(SELECT count(*) FROM capital_policy_requests "
     "WHERE state <> 'requested' AND processed_at_ms IS NULL) + "
     "(SELECT count(*) FROM uncertainty_resolution_requests "
     "WHERE state <> 'requested' AND processed_at_ms IS NULL)"),
    ("trading request linked to a trading state of another scope",
     "SELECT count(*) FROM trading_control_requests r JOIN trading_state s "
     "ON s.id = r.trading_state_id WHERE (s.exchange_account_id, s.deployment_environment) "
     "IS DISTINCT FROM (r.exchange_account_id, r.deployment_environment)"),
    ("capital request linked to a revision of another scope",
     "SELECT count(*) FROM capital_policy_requests r JOIN capital_policy_revisions v "
     "ON v.id = r.policy_revision_id "
     "WHERE (v.exchange_account_id, v.deployment_environment, v.symbol) "
     "IS DISTINCT FROM (r.exchange_account_id, r.deployment_environment, r.symbol)"),
    ("uncertainty request named by a journal row of another scope",
     "SELECT count(*) FROM uncertainty_resolution_requests r JOIN execution_resolution_journal j "
     "ON j.operator_request_id = r.request_id "
     "WHERE (j.exchange_account_id, j.deployment_environment) "
     "IS DISTINCT FROM (r.exchange_account_id, r.deployment_environment)"),
    ("uncertainty request named by a journal row of another subject",
     "SELECT count(*) FROM uncertainty_resolution_requests r JOIN execution_resolution_journal j "
     "ON j.operator_request_id = r.request_id "
     "WHERE coalesce(j.attempt_id, j.quarantine_id) <> r.uncertainty_id"),
    ("cancel-all attempt of another scope than its trading state",
     "SELECT count(DISTINCT a.attempt_id) FROM funding_cancel_all_audit a JOIN trading_state s "
     "ON s.id = a.trading_state_id WHERE (s.exchange_account_id, s.deployment_environment) "
     "IS DISTINCT FROM (a.exchange_account_id, a.deployment_environment)"),
    ("revision source.request_id that is not a request id",
     "SELECT count(*) FROM capital_policy_revisions WHERE source ? 'request_id' "
     f"AND coalesce(source ->> 'request_id', '') !~ {_UUID_TEXT}"),
    ("revision source.request_id naming no request of its scope and currency",
     "SELECT count(*) FROM capital_policy_revisions v WHERE v.source ? 'request_id' "
     f"AND v.source ->> 'request_id' ~ {_UUID_TEXT} AND NOT EXISTS ("
     "SELECT FROM capital_policy_requests r WHERE r.request_id = (v.source ->> 'request_id')::uuid "
     "AND r.exchange_account_id = v.exchange_account_id "
     "AND r.deployment_environment = v.deployment_environment AND r.symbol = v.symbol)"),
    ("request named by the source of more than one revision",
     "SELECT count(*) FROM (SELECT source ->> 'request_id' FROM capital_policy_revisions "
     "WHERE source ? 'request_id' GROUP BY 1 HAVING count(*) > 1) AS twice"),
    ("currency cancelled more than once inside one kill's window",
     f"SELECT count(*) FROM (SELECT FROM ({_KILL_ATTEMPTS}) AS m "
     "GROUP BY m.request_id, m.currency HAVING count(*) > 1) AS twice"),
)

POSTCONDITIONS: tuple[tuple[str, str], ...] = (
    ("revision whose typed request id differs from source.request_id",
     "SELECT count(*) FROM capital_policy_revisions WHERE "
     "(source ->> 'request_id') IS DISTINCT FROM operator_request_id::text"),
    ("operator rows written by an applied request, minus trading rows carrying a request",
     f"SELECT (SELECT count(DISTINCT s.id) {_TRADING_MATCH}) - "
     "(SELECT count(*) FROM trading_state WHERE operator_request_id IS NOT NULL)"),
    # The writer wrote its row while it was being applied; a request that restated the row
    # was created after it (one kill and one other request may wait at a time).
    ("trading row outside its request's creation-to-processing interval",
     "SELECT count(*) FROM trading_state s JOIN trading_control_requests r "
     "ON r.request_id = s.operator_request_id "
     "WHERE NOT (r.created_at_ms <= s.created_at_ms AND s.created_at_ms <= r.processed_at_ms)"),
    ("cancel-all attempt whose rows name different requests",
     "SELECT count(*) FROM (SELECT attempt_id FROM funding_cancel_all_audit GROUP BY attempt_id "
     "HAVING count(DISTINCT operator_request_id) > 1 "
     "OR (count(operator_request_id) > 0 AND count(operator_request_id) < count(*))) AS split"),
    ("append-only trigger left disabled",
     "SELECT count(*) FROM pg_trigger WHERE tgname IN ("
     + ", ".join(f"'{name}'" for name in APPEND_ONLY_TRIGGERS.values())
     + ") AND tgrelid IN ("
     + ", ".join(f"'public.{table}'::regclass" for table in APPEND_ONLY_TRIGGERS)
     + ") AND tgenabled <> 'O'"),
)


def _role_exists(name: str) -> bool:
    return bool(op.get_bind().execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}).scalar())


def _violations(checks: tuple[tuple[str, str], ...]) -> list[str]:
    conn = op.get_bind()
    found = [(what, conn.execute(text(sql)).scalar_one()) for what, sql in checks]
    return [f"{what}: {count}" for what, count in found if count]


def _check_preconditions() -> None:
    violations = _violations(PRECONDITIONS)
    if violations:
        raise RuntimeError(
            "refuse to link operator request effects; rows that break a precondition: "
            + "; ".join(violations)
        )


def _check_postconditions() -> None:
    violations = _violations(POSTCONDITIONS)
    if violations:
        raise RuntimeError("operator request effect back-fill does not match: "
                           + "; ".join(violations))


def _add_effect_links() -> None:
    for request, (name, columns) in REQUEST_SCOPE_KEYS.items():
        op.execute(f"ALTER TABLE public.{request} ADD CONSTRAINT {name} UNIQUE ({columns})")
    for effect, (request, columns, fk, unique) in EFFECT_KEYS.items():
        op.execute(f"ALTER TABLE public.{effect} ADD COLUMN operator_request_id uuid NULL")
        op.execute(
            f"ALTER TABLE public.{effect} ADD CONSTRAINT {fk} FOREIGN KEY ({columns}) "
            f"REFERENCES public.{request} ({REQUEST_SCOPE_KEYS[request][1]}) ON DELETE RESTRICT"
        )
        if unique is not None:
            op.execute(f"CREATE UNIQUE INDEX {unique} ON public.{effect} (operator_request_id) "
                       "WHERE operator_request_id IS NOT NULL")
    op.execute(f"ALTER TABLE public.execution_resolution_journal DROP CONSTRAINT {_JOURNAL_LEGACY_FK}")
    for name, subject in JOURNAL_FKS.items():
        op.execute(
            f"ALTER TABLE public.execution_resolution_journal ADD CONSTRAINT {name} FOREIGN KEY "
            f"(operator_request_id, exchange_account_id, deployment_environment, {subject}) "
            "REFERENCES public.uncertainty_resolution_requests "
            f"({REQUEST_SCOPE_KEYS['uncertainty_resolution_requests'][1]}) ON DELETE RESTRICT"
        )
    op.execute(f"ALTER TABLE public.trading_state ADD CONSTRAINT {TRADING_STATE_SCOPE_KEY} "
               "UNIQUE (id, exchange_account_id, deployment_environment)")
    op.execute(f"ALTER TABLE public.funding_cancel_all_audit DROP CONSTRAINT {AUDIT_TRADING_STATE_FK}")
    op.execute(
        f"ALTER TABLE public.funding_cancel_all_audit ADD CONSTRAINT {AUDIT_TRADING_STATE_FK} "
        "FOREIGN KEY (trading_state_id, exchange_account_id, deployment_environment) "
        "REFERENCES public.trading_state (id, exchange_account_id, deployment_environment) "
        "ON DELETE RESTRICT"
    )


def _backfill() -> None:
    for effect, trigger in APPEND_ONLY_TRIGGERS.items():
        op.execute(f"ALTER TABLE public.{effect} DISABLE TRIGGER {trigger}")
    op.execute("UPDATE public.capital_policy_revisions "
               "SET operator_request_id = (source ->> 'request_id')::uuid "
               "WHERE source ? 'request_id'")
    op.execute(
        "UPDATE public.trading_state t SET operator_request_id = m.request_id FROM ("
        "SELECT DISTINCT ON (s.id) s.id, r.request_id "
        f"{_TRADING_MATCH} ORDER BY s.id, r.processed_at_ms, r.request_id) AS m "
        "WHERE t.id = m.id"
    )
    op.execute(
        "UPDATE public.funding_cancel_all_audit a SET operator_request_id = m.request_id "
        f"FROM ({_KILL_ATTEMPTS}) AS m WHERE a.attempt_id = m.attempt_id"
    )
    for effect, trigger in APPEND_ONLY_TRIGGERS.items():
        op.execute(f"ALTER TABLE public.{effect} ENABLE TRIGGER {trigger}")


def _request_guard(name: str, transition: str, *, journal: bool = False) -> None:
    """``state`` leaves ``requested`` exactly once; which column a role may change is a grant."""
    g2 = """
        IF NEW.state = 'applied' AND NOT EXISTS (
          SELECT 1 FROM public.execution_resolution_journal j
          WHERE j.operator_request_id = NEW.request_id)
          THEN RAISE EXCEPTION 'ledger resolution request applied without a journal row'; END IF;"""
    op.execute(f"""CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION '{transition}'; END IF;{g2 if journal else ''}
        RETURN NEW; END $$""")


def _close_product_columns() -> None:
    for table, column in CLOSED_COLUMNS.items():
        if _role_exists(_WRITER):
            op.execute(f"REVOKE UPDATE ({column}) ON public.{table} FROM {_WRITER}")
        check = OUTCOME_CHECKS[table]
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {check}")
        op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {check} CHECK ({OUTCOME_SHAPE})")


def _check_runtime_updates() -> None:
    """No runtime role may UPDATE what the operator asked, nor a closed product column."""
    conn = op.get_bind()
    columns = {table: list(names) for table, names in REQUEST_COLUMNS.items()}
    for table, column in CLOSED_COLUMNS.items():
        columns[table].append(column)
    granted = []
    for role in (_WRITER, _READER):
        if not _role_exists(role):
            continue
        for table, names in columns.items():
            for column in names:
                if conn.execute(text("SELECT has_column_privilege(:role, :table, :column, 'UPDATE')"),
                                {"role": role, "table": f"public.{table}",
                                 "column": column}).scalar_one():
                    granted.append(f"{role} {table}.{column}")
    if granted:
        raise RuntimeError("a runtime role may UPDATE a request or closed column: "
                           + ", ".join(granted))


# G1 (7d2a9c4e6b13): the runtime role's revision must apply a waiting request for exactly this
# change, named by the typed operator_request_id.
_RUNTIME_REVISION_GUARD = """CREATE OR REPLACE FUNCTION public.guard_runtime_policy_revision()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.capital_policy_revisions%ROWTYPE;
            req public.capital_policy_requests%ROWTYPE;
    BEGIN
      IF pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RETURN NEW;  -- the owner: the amendment script and migrations
      END IF;
      SELECT r.* INTO prev FROM public.capital_policy_heads h
        JOIN public.capital_policy_revisions r ON r.id = h.revision_id
        WHERE h.exchange_account_id = NEW.exchange_account_id
          AND h.deployment_environment = NEW.deployment_environment AND h.symbol = NEW.symbol;
      IF NOT FOUND OR NEW.revision <> prev.revision + 1
         OR NEW.schema_version <> prev.schema_version
         OR jsonb_typeof(NEW.policy -> 'enabled') IS DISTINCT FROM 'boolean'
         OR (NEW.policy - 'enabled') IS DISTINCT FROM (prev.policy - 'enabled') THEN
        RAISE EXCEPTION 'runtime policy revision may only toggle enabled of the current revision';
      END IF;
      -- ...and only as the application of a waiting operator request for
      -- exactly this change, by an operator who is still authorized.
      SELECT q.* INTO req FROM public.capital_policy_requests q
        WHERE q.request_id = NEW.operator_request_id AND q.state = 'requested'
          AND q.exchange_account_id = NEW.exchange_account_id
          AND q.deployment_environment = NEW.deployment_environment AND q.symbol = NEW.symbol
          AND q.action = CASE WHEN (NEW.policy ->> 'enabled')::boolean THEN 'enable' ELSE 'disable' END;
      IF NOT FOUND OR NOT public.operator_authorized(req.exchange_account_id, req.requested_by) THEN
        RAISE EXCEPTION 'runtime policy revision must apply a waiting request of an authorized operator';
      END IF;
      RETURN NEW;
    END $$"""


def upgrade() -> None:
    _check_preconditions()
    _add_effect_links()
    _backfill()
    _check_postconditions()

    _request_guard("guard_trading_control_request", "invalid trading control request transition")
    _request_guard("guard_capital_policy_request", "invalid capital policy request transition")
    _request_guard("guard_uncertainty_resolution_request",
                   "invalid uncertainty resolution transition", journal=True)
    _close_product_columns()
    _check_runtime_updates()
    op.execute(_RUNTIME_REVISION_GUARD)


def downgrade() -> None:
    raise RuntimeError(
        "5e820d6dc7da is forward-only (docs/adr/2026-10-08-forward-only-migrations.md): "
        "restore the pre-migration backup to roll back"
    )
