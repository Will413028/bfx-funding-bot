"""Drop ``deployment_environment`` and the per-row realm trigger from every live table.

A database holds one realm (``database_realm``, a7c3e9f1b2d4), so on the 31 live tables
(``public`` and ``projection_audit.runs``) the column repeats the stamp on every row and the
``database_realm_write`` trigger only re-checks that. What remains of the realm is the stamp
row and the boot-time ``assert_database_realm`` (docs/plans/2026-11-08-drop-realm-columns.md,
step 4a; the code that stops writing the column ships in the same release, D3).

In order, all in the revision's transaction:

1. Assert: a live realm-guarded table (one carrying the ``guard_database_realm`` trigger,
   outside the two archive schemas) holding a row whose realm is not the stamp refuses the
   upgrade; any row in an unstamped database refuses with its own message. Tables come from
   ``pg_catalog`` and the probe is one statement, so the answer is from one snapshot. This is
   also why every narrowed key below can be rebuilt: two rows differing only by realm cannot
   exist. The same is asserted of ``legacy_archive.event_log``, read only: it is the one
   archive table the application reads (``modules/execution/archived_execution_history.py``),
   and from this release that read no longer filters on the realm (plan step 4e-2, D7).
2. Record a digest of every archive table (``legacy_archive``, ``release_archive``).
3. Drop ``database_realm_write`` on the 31 live tables and the 12 ``legacy_archive`` tables
   (only the trigger; ``archive_frozen`` and the archived rows are untouched, the archive keeps
   its column).
4. Drop the 6 composite foreign keys and the 2 CHECKs that name the column.
5. ``CREATE OR REPLACE`` the 9 live trigger functions whose body reads the column, so they
   compare scope on ``exchange_account_id`` only. ``guard_trading_state_transition``'s advisory
   lock key becomes ``'bfx-trading-state:' || account`` (D6: lock keys no longer carry the env).
   Owner, ``SET search_path`` and grants are kept by ``CREATE OR REPLACE``.
6. ``DROP COLUMN`` on the 31 tables. Keys and indexes that include the column, and its
   column-level grants, go with it.
7. Rebuild every dropped key and index under its original name without the column, except
   ``ix_ledger_observation_query_scope_revision`` (the narrow unique key read backward) and
   ``idx_execution_decisions_env_occurred``'s realm prefix (rebuilt on ``occurred_at_ms``).
   ``ck_trading_state_evidence`` is rebuilt without its realm term; ``ck_sim_venue_event_realm``
   is not rebuilt (the simulated venue refuses a non-``ci`` database itself:
   ``modules/simulated_venue/_internal/store_sql.py`` ``_check_database_realm``).
8. Rebuild the 6 foreign keys under their original names on the narrow keys.
9. ``DROP FUNCTION public.guard_database_realm()``.
10. Assert the end state: no live column, no ``database_realm_write`` trigger, no
    ``guard_database_realm``, no non-archive function body naming the column, and every archive
    table's digest equal to step 2.

squawk: ``DROP COLUMN``, ``ADD PRIMARY KEY``/``UNIQUE``/``FOREIGN KEY``/``CHECK`` without
``NOT VALID``, non-``CONCURRENTLY`` indexes and the ``DROP FUNCTION`` are each marked
``squawk-ignore``. Those rules guard against blocking writers on a live table or breaking a
client that still calls the function (its 43 triggers are dropped first); bfx-deploy stops the webapi and every writer
while migrations run, ``CONCURRENTLY`` cannot run inside the revision's transaction, and
``NOT VALID`` plus ``VALIDATE`` in one transaction would not shorten the lock either
(precedents: a9d2c4e6f813, 96535191e443).

The downgrade raises: the realm values are gone and nothing records which rows had which
(docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 545b2877e9bd
Revises: 96535191e443
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "545b2877e9bd"
down_revision = "96535191e443"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

COLUMN = "deployment_environment"
ARCHIVE_SCHEMAS = ("legacy_archive", "release_archive")

LIVE_TABLES: tuple[str, ...] = (
    "projection_audit.runs",
    "public.accepted_capital_basis",
    "public.attribution_legacy_offer_cells",
    "public.attribution_weekly",
    "public.bot_runs",
    "public.capital_command_clock",
    "public.capital_policy_heads",
    "public.capital_policy_requests",
    "public.capital_policy_revisions",
    "public.config_regime",
    "public.credential_rotation_recipients",
    "public.credential_rotation_requests",
    "public.diagnostics",
    "public.execution_decisions",
    "public.execution_resolution_journal",
    "public.funding_cancel_all_audit",
    "public.funding_credit_history",
    "public.funding_interest_payments",
    "public.funding_trades",
    "public.ledger_observation",
    "public.ledger_observation_query",
    "public.nav_peak",
    "public.nav_window_samples",
    "public.quarantine_opening",
    "public.sim_venue_event",
    "public.submission_attempt_journal",
    "public.trading_control_requests",
    "public.trading_state",
    "public.uncertainty_resolution_requests",
    "public.venue_credit_mirror",
    "public.venue_offer_mirror",
)

LEGACY_ARCHIVE_GUARDED: tuple[str, ...] = (
    "capital_snapshot_queries",
    "capital_snapshots",
    "event_log",
    "event_prefix_hashes",
    "execution_uncertainties",
    "offer_claims",
    "position_state",
    "projection_heads",
    "reconcile_observation",
    "submission_attempts",
    "venue_credit_state",
    "venue_offer_state",
)

# (table, name, columns) — constraints rebuilt with ALTER TABLE ... ADD CONSTRAINT.
PRIMARY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("attribution_legacy_offer_cells", "attribution_legacy_offer_cells_pkey",
     "exchange_account_id, venue_offer_id, cell"),
    ("attribution_weekly", "attribution_weekly_pkey", "exchange_account_id, cell, week_start_ms"),
    ("capital_command_clock", "capital_command_clock_pkey", "exchange_account_id"),
    ("capital_policy_heads", "capital_policy_heads_pkey", "exchange_account_id, symbol"),
    ("config_regime", "config_regime_pkey", "exchange_account_id, recorded_at_ms"),
    ("nav_peak", "nav_peak_pkey", "exchange_account_id, symbol"),
    ("sim_venue_event", "pk_sim_venue_event", "exchange_account_id, seq"),
    ("venue_credit_mirror", "venue_credit_mirror_pkey",
     "exchange_account_id, venue_credit_id, source_kind"),
    ("venue_offer_mirror", "venue_offer_mirror_pkey", "exchange_account_id, venue_offer_id"),
)
UNIQUE_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("capital_policy_requests", "uq_capital_policy_requests_scope",
     "request_id, exchange_account_id, symbol"),
    ("capital_policy_revisions", "uq_capital_policy_revision_scope",
     "exchange_account_id, symbol, revision"),
    ("ledger_observation_query", "uq_ledger_observation_query_scope_revision",
     "exchange_account_id, query_revision"),
    ("submission_attempt_journal", "uq_submission_attempt_scope_seq",
     "exchange_account_id, attempt_seq"),
    ("trading_control_requests", "uq_trading_control_requests_scope",
     "request_id, exchange_account_id"),
    ("uncertainty_resolution_requests", "uq_uncertainty_resolution_requests_scope",
     "request_id, exchange_account_id, uncertainty_id"),
)

_REQUESTED = "state = 'requested'::text"
# (table, name, unique, columns, predicate) — rebuilt with CREATE [UNIQUE] INDEX.
INDEXES: tuple[tuple[str, str, bool, str, str | None], ...] = (
    ("accepted_capital_basis", "ix_accepted_capital_basis_scope_accepted", False,
     "exchange_account_id, accepted_at_ms DESC", None),
    ("bot_runs", "ix_bot_runs_scope_started", False, "exchange_account_id, started_at_ms", None),
    ("capital_policy_requests", "ix_capital_policy_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("capital_policy_requests", "uq_capital_policy_requests_pending", True,
     "exchange_account_id, symbol, action", _REQUESTED),
    ("credential_rotation_recipients", "ix_credential_rotation_recipients_run", False,
     "exchange_account_id, run_id, created_at_ms", None),
    ("credential_rotation_requests", "ix_credential_rotation_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("credential_rotation_requests", "uq_credential_rotation_requests_pending", True,
     "exchange_account_id", _REQUESTED),
    ("execution_decisions", "idx_execution_decisions_env_occurred", False, "occurred_at_ms", None),
    ("execution_resolution_journal", "ix_execution_resolution_scope_resolved", False,
     "exchange_account_id, resolved_at_ms DESC, id DESC", None),
    ("funding_cancel_all_audit", "ix_funding_cancel_all_audit_scope_id", False,
     "exchange_account_id, id", None),
    ("funding_trades", "ix_funding_trades_scope_symbol_created", False,
     "exchange_account_id, symbol, mts_create", None),
    ("ledger_observation", "ix_ledger_observation_scope_finished", False,
     "exchange_account_id, query_finished_at_ms DESC, id", None),
    ("nav_window_samples", "ix_nav_window_samples_scope_symbol_time", False,
     "exchange_account_id, symbol, occurred_at_ms", None),
    ("quarantine_opening", "ix_quarantine_opening_scope_revision", False,
     "exchange_account_id, opened_revision", None),
    ("trading_control_requests", "ix_trading_control_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("trading_control_requests", "uq_trading_control_requests_pending", True,
     "exchange_account_id", f"{_REQUESTED} AND action <> 'kill'::text"),
    ("trading_control_requests", "uq_trading_control_requests_pending_kill", True,
     "exchange_account_id", f"{_REQUESTED} AND action = 'kill'::text"),
    ("trading_state", "ix_trading_state_scope_id", True, "exchange_account_id, id", None),
    ("uncertainty_resolution_requests", "ix_uncertainty_resolution_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("uncertainty_resolution_requests", "uq_uncertainty_resolution_requests_pending", True,
     "exchange_account_id, uncertainty_id", _REQUESTED),
    ("venue_credit_mirror", "ix_venue_credit_mirror_live", False,
     "exchange_account_id, symbol", "present_in_latest_accepted_snapshot"),
    ("venue_offer_mirror", "ix_venue_offer_mirror_live", False,
     "exchange_account_id, symbol", "present_in_latest_accepted_snapshot"),
)

# (table, name, columns, referenced table, referenced columns); all ON DELETE RESTRICT.
FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    ("capital_policy_revisions", "fk_capital_policy_revisions_operator_request",
     "operator_request_id, exchange_account_id, symbol",
     "capital_policy_requests", "request_id, exchange_account_id, symbol"),
    ("execution_resolution_journal", "fk_execution_resolution_journal_request_attempt",
     "operator_request_id, exchange_account_id, attempt_id",
     "uncertainty_resolution_requests", "request_id, exchange_account_id, uncertainty_id"),
    ("execution_resolution_journal", "fk_execution_resolution_journal_request_quarantine",
     "operator_request_id, exchange_account_id, quarantine_id",
     "uncertainty_resolution_requests", "request_id, exchange_account_id, uncertainty_id"),
    ("funding_cancel_all_audit", "fk_funding_cancel_all_audit_operator_request",
     "operator_request_id, exchange_account_id",
     "trading_control_requests", "request_id, exchange_account_id"),
    ("funding_cancel_all_audit", "fk_funding_cancel_all_audit_trading_state",
     "trading_state_id, exchange_account_id",
     "trading_state", "id, exchange_account_id"),
    ("trading_state", "fk_trading_state_operator_request",
     "operator_request_id, exchange_account_id",
     "trading_control_requests", "request_id, exchange_account_id"),
)

CHECKS_DROPPED: tuple[tuple[str, str], ...] = (
    ("trading_state", "ck_trading_state_evidence"),
    ("sim_venue_event", "ck_sim_venue_event_realm"),
)
TRADING_STATE_EVIDENCE = (
    "length(TRIM(BOTH FROM actor)) > 0 AND length(TRIM(BOTH FROM reason)) > 0 "
    "AND created_at_ms >= 0"
)

# The nine live trigger functions whose body read the column, rewritten to compare scope on
# exchange_account_id only. Bodies are otherwise the ones at 96535191e443.
FUNCTIONS: dict[str, str] = {
    "projection_audit.guard_run": """
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.complete THEN RAISE EXCEPTION 'archive must start incomplete'; END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' OR OLD.complete THEN
            RAISE EXCEPTION 'immutable archive run';
          END IF;
          IF NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.exchange_account_id IS DISTINCT FROM OLD.exchange_account_id
             OR NOT NEW.complete THEN
            RAISE EXCEPTION 'archive only permits completion';
          END IF;
          RETURN NEW;
        END""",
    "public.guard_credential_rotation_request": """ BEGIN
        IF (NEW.request_id,NEW.exchange_account_id,NEW.recipient_id,NEW.replaces_credential_id,NEW.sealed_credential,NEW.label,NEW.reason,NEW.requested_by,NEW.created_at_ms)
          IS DISTINCT FROM (OLD.request_id,OLD.exchange_account_id,OLD.recipient_id,OLD.replaces_credential_id,OLD.sealed_credential,OLD.label,OLD.reason,OLD.requested_by,OLD.created_at_ms)
          THEN RAISE EXCEPTION 'immutable credential rotation request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid credential rotation request transition'; END IF;
        RETURN NEW; END""",
    "public.guard_ledger_mirror": """
        BEGIN
          IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
            RAISE EXCEPTION 'immutable ledger mirror deletion';
          END IF;
          IF OLD.terminal_evidence_id IS NOT NULL
             AND (NEW.terminal_evidence_id IS DISTINCT FROM OLD.terminal_evidence_id
               OR NEW.terminal_kind IS DISTINCT FROM OLD.terminal_kind
               OR NEW.present_in_latest_accepted_snapshot) THEN
            RAISE EXCEPTION 'immutable ledger terminal evidence';
          END IF;
          IF NEW.exchange_account_id IS DISTINCT FROM OLD.exchange_account_id THEN
            RAISE EXCEPTION 'immutable ledger mirror scope';
          END IF;
          RETURN NEW;
        END""",
    "public.guard_ledger_observation_accept": """
        DECLARE query_row record;
        BEGIN
          SELECT exchange_account_id, query_revision,
                 started_at_ms, start_revision INTO query_row
            FROM public.ledger_observation_query WHERE query_id = NEW.query_id;
          IF NOT FOUND OR
             query_row.exchange_account_id IS DISTINCT FROM NEW.exchange_account_id
             OR NEW.query_finished_at_ms < query_row.started_at_ms THEN
            RAISE EXCEPTION 'ledger observation query scope mismatch';
          END IF;
          IF NEW.accepted AND (
             NEW.accept_revision IS DISTINCT FROM query_row.start_revision
             OR NOT EXISTS (
               SELECT 1 FROM public.capital_command_clock c
               WHERE (c.exchange_account_id, c.revision)
                 = (NEW.exchange_account_id, NEW.accept_revision))
             OR query_row.query_revision IS DISTINCT FROM (
               SELECT max(q.query_revision) FROM public.ledger_observation_query q
               WHERE q.exchange_account_id = NEW.exchange_account_id)) THEN
            RAISE EXCEPTION 'ledger observation accept fence';
          END IF;
          RETURN NEW;
        END""",
    "public.guard_ledger_quarantine_source": """
        BEGIN
          IF NEW.source_attempt_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.submission_attempt_journal a
            WHERE a.attempt_id = NEW.source_attempt_id
              AND (a.exchange_account_id, a.symbol)
                  IS DISTINCT FROM
                  (NEW.exchange_account_id, NEW.symbol)
          ) THEN
            RAISE EXCEPTION 'ledger quarantine source scope mismatch';
          END IF;
          RETURN NEW;
        END""",
    "public.guard_ledger_scope": """
        DECLARE parent_scope record;
        BEGIN
          IF TG_TABLE_NAME = 'submission_attempt_journal' THEN
            SELECT exchange_account_id, symbol, cell_id
              INTO parent_scope
              FROM public.execution_decisions WHERE decision_id = NEW.execution_decision_id;
            IF parent_scope.exchange_account_id IS NULL
               OR (parent_scope.exchange_account_id, parent_scope.symbol,
                   parent_scope.cell_id)
                  IS DISTINCT FROM (NEW.exchange_account_id, NEW.symbol, NEW.cell_id) THEN
              RAISE EXCEPTION 'ledger decision scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'quarantine_member' THEN
            SELECT q.exchange_account_id, q.symbol INTO parent_scope
              FROM public.quarantine_opening q WHERE q.quarantine_id = NEW.quarantine_id;
            IF NOT EXISTS (
              SELECT 1 FROM public.ledger_observation o
              JOIN (
                SELECT observation_id, 'offer'::text AS source_kind,
                       venue_offer_id AS venue_object_id, symbol
                  FROM public.ledger_observation_offer
                UNION ALL
                SELECT observation_id, 'offer'::text, venue_offer_id, symbol
                  FROM public.ledger_observation_offer_history
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit_history
              ) h ON h.observation_id = o.id
              WHERE o.id = NEW.observation_id
                AND h.source_kind = NEW.source_kind
                AND h.venue_object_id = NEW.venue_object_id
                AND (o.exchange_account_id, h.symbol)
                    = (parent_scope.exchange_account_id, parent_scope.symbol)) THEN
              RAISE EXCEPTION 'ledger member scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_resolution_journal' THEN
            IF NEW.attempt_id IS NOT NULL THEN
              SELECT exchange_account_id, symbol INTO parent_scope
                FROM public.submission_attempt_journal WHERE attempt_id = NEW.attempt_id;
            ELSE
              SELECT exchange_account_id, symbol INTO parent_scope
                FROM public.quarantine_opening WHERE quarantine_id = NEW.quarantine_id;
            END IF;
            IF (parent_scope.exchange_account_id, parent_scope.symbol)
                 IS DISTINCT FROM (NEW.exchange_account_id, NEW.symbol)
               OR NOT EXISTS (SELECT 1 FROM public.ledger_observation o
                 WHERE o.id = NEW.observation_id
                 AND o.exchange_account_id = NEW.exchange_account_id) THEN
              RAISE EXCEPTION 'ledger resolution scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'accepted_capital_basis' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o WHERE o.id = NEW.observation_id
              AND o.exchange_account_id = NEW.exchange_account_id
              AND o.accept_revision = NEW.accept_revision) THEN
              RAISE EXCEPTION 'ledger basis scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_offer_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND o.exchange_account_id = NEW.exchange_account_id)
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_offer_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_offer_id = NEW.venue_offer_id
                  AND h.symbol = NEW.symbol AND h.terminal_kind = NEW.terminal_kind
                  AND o.exchange_account_id = NEW.exchange_account_id)) THEN
              RAISE EXCEPTION 'ledger offer mirror scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_credit_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND o.exchange_account_id = NEW.exchange_account_id)
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_credit_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_credit_id = NEW.venue_credit_id
                  AND h.source_kind = NEW.source_kind AND h.symbol = NEW.symbol
                  AND h.terminal_kind = NEW.terminal_kind
                  AND o.exchange_account_id = NEW.exchange_account_id)) THEN
              RAISE EXCEPTION 'ledger credit mirror scope mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END""",
    "public.guard_runtime_policy_head": """
    BEGIN
      IF pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RETURN NEW;
      END IF;
      IF NEW.revision <> OLD.revision + 1 OR NOT EXISTS (
          SELECT FROM public.capital_policy_revisions r
          WHERE r.id = NEW.revision_id AND r.revision = NEW.revision
            AND r.exchange_account_id = OLD.exchange_account_id
            AND r.symbol = OLD.symbol) THEN
        RAISE EXCEPTION 'runtime policy head may only advance to the next revision of its scope';
      END IF;
      RETURN NEW;
    END""",
    "public.guard_runtime_policy_revision": """
    DECLARE prev public.capital_policy_revisions%ROWTYPE;
            req public.capital_policy_requests%ROWTYPE;
    BEGIN
      IF pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RETURN NEW;  -- the owner: the amendment script and migrations
      END IF;
      SELECT r.* INTO prev FROM public.capital_policy_heads h
        JOIN public.capital_policy_revisions r ON r.id = h.revision_id
        WHERE h.exchange_account_id = NEW.exchange_account_id AND h.symbol = NEW.symbol;
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
          AND q.exchange_account_id = NEW.exchange_account_id AND q.symbol = NEW.symbol
          AND q.action = CASE WHEN (NEW.policy ->> 'enabled')::boolean THEN 'enable' ELSE 'disable' END;
      IF NOT FOUND OR NOT public.operator_authorized(req.exchange_account_id, req.requested_by) THEN
        RAISE EXCEPTION 'runtime policy revision must apply a waiting request of an authorized operator';
      END IF;
      RETURN NEW;
    END""",
    "public.guard_trading_state_transition": """
    DECLARE prev public.trading_state%ROWTYPE;
            db_now bigint := (extract(epoch FROM clock_timestamp()) * 1000)::bigint;
    BEGIN
      -- The latest row is read after the lock below; only a READ COMMITTED snapshot
      -- is taken after it.
      IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'trading_state is written under READ COMMITTED only'
          USING ERRCODE = 'BX004';
      END IF;
      -- One realm per database (a7c3e9f1b2d4): the key is the account alone (D6).
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'bfx-trading-state:' || NEW.exchange_account_id::text, 0));
      -- Assigned under the scope lock: a row that waited here must not keep an
      -- id drawn before the row it waited for, or "highest id" would not be
      -- the latest decision.
      NEW.id := nextval('public.trading_state_id_seq');
      SELECT * INTO prev FROM public.trading_state
        WHERE exchange_account_id = NEW.exchange_account_id
        ORDER BY id DESC LIMIT 1;
      IF NEW.cause = 'auto' THEN
        NEW.created_at_ms := db_now;
      END IF;
      IF NEW.state = 'ACTIVE' AND NEW.cause = 'auto' THEN
        IF NOT FOUND OR prev.state <> 'HALTED' OR prev.cause <> 'auto' THEN
          RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by auto',
            coalesce(prev.state || '/' || prev.cause, 'none') USING ERRCODE = 'BX001';
        END IF;
        IF db_now - prev.created_at_ms < 900000 THEN
          RAISE EXCEPTION 'auto resume before the minimum halt duration' USING ERRCODE = 'BX002';
        END IF;
        IF (SELECT count(*) FROM public.trading_state
              WHERE exchange_account_id = NEW.exchange_account_id
                AND state = 'ACTIVE' AND cause = 'auto'
                AND created_at_ms > db_now - 86400000
            ) >= 2 THEN
          RAISE EXCEPTION 'auto resume limit reached for the window' USING ERRCODE = 'BX003';
        END IF;
      ELSIF FOUND AND prev.state <> 'ACTIVE' AND NEW.state = 'ACTIVE' AND NEW.cause <> 'operator' THEN
        RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by %', prev.state, NEW.cause;
      END IF;
      RETURN NEW;
    END""",
}

# One row per live realm-guarded table that holds a row of another realm than the stamp. Without
# a stamp the scalar subquery is NULL and every row is distinct from it.
_PROBE = """
SELECT string_agg(
  format('SELECT %L AS t WHERE EXISTS (SELECT 1 FROM %I.%I WHERE deployment_environment '
         'IS DISTINCT FROM (SELECT realm FROM public.database_realm))',
         format('%I.%I', n.nspname, c.relname), n.nspname, c.relname),
  ' UNION ALL ' ORDER BY n.nspname, c.relname)
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE t.tgfoid = 'public.guard_database_realm'::regproc AND NOT t.tgisinternal
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname <> ALL (:archives)
"""

# Every ordinary table in the archive schemas; the digest is over the rows as JSON, so it moves
# if any byte of any row does.
_ARCHIVE_TABLES = """
SELECT format('%I.%I', n.nspname, c.relname) FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (:archives) AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY 1
"""


def _digest(qualified: str) -> str:
    # Per-row hashes, then a hash of their ordered concatenation: the aggregated text grows by
    # 64 bytes a row instead of a row's whole JSON, far from the 1 GB text limit.
    return ("SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(h, '' ORDER BY h), ''), "
            "'UTF8')), 'hex') FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), "
            f"'hex') AS h FROM {qualified} t) r")


def _refuse_other_realms_in_the_read_archive() -> None:
    """The web API's archive read stops filtering on the realm, so no other may be there."""
    foreign = op.get_bind().execute(text(
        "SELECT count(*) FROM legacy_archive.event_log WHERE deployment_environment "
        "IS DISTINCT FROM (SELECT realm FROM public.database_realm)")).scalar() or 0
    if foreign:
        raise RuntimeError(
            f"refuse: {foreign} rows of legacy_archive.event_log carry a realm other than the "
            "database_realm stamp (or the database is not stamped); the archive read no longer "
            "filters on it")


def _archive_digests() -> dict[str, tuple[int, str]]:
    bind = op.get_bind()
    tables = bind.execute(text(_ARCHIVE_TABLES), {"archives": list(ARCHIVE_SCHEMAS)}).scalars()
    digests: dict[str, tuple[int, str]] = {}
    for table in list(tables):
        count, digest = bind.exec_driver_sql(_digest(table)).one()
        digests[table] = (int(count), str(digest))
    return digests


def _refuse_other_realms() -> None:
    bind = op.get_bind()
    probe = bind.execute(text(_PROBE), {"archives": list(ARCHIVE_SCHEMAS)}).scalar()
    if not probe:
        return
    holding = list(bind.exec_driver_sql(str(probe)).scalars())
    if not holding:
        return
    if bind.execute(text("SELECT count(*) FROM public.database_realm")).scalar():
        raise RuntimeError(
            "refuse: rows of a realm other than the database_realm stamp in "
            + ", ".join(holding))
    raise RuntimeError(
        "refuse: this database is not stamped (database_realm is empty) but holds rows in "
        + ", ".join(holding))


def _assert_end_state(before: dict[str, tuple[int, str]]) -> None:
    bind = op.get_bind()
    archives = {"archives": list(ARCHIVE_SCHEMAS)}
    columns = list(bind.execute(text(
        "SELECT table_schema || '.' || table_name FROM information_schema.columns "
        "WHERE column_name = :column AND table_schema <> ALL (:archives) "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
    ), {"column": COLUMN, **archives}).scalars())
    if columns:
        raise RuntimeError(f"refuse: {COLUMN} still on {', '.join(columns)}")
    triggers = int(bind.execute(text(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'database_realm_write'")).scalar() or 0)
    if triggers:
        raise RuntimeError(f"refuse: {triggers} database_realm_write triggers remain")
    if bind.execute(text("SELECT to_regprocedure('public.guard_database_realm()')")).scalar():
        raise RuntimeError("refuse: public.guard_database_realm() still exists")
    readers = list(bind.execute(text(
        "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE p.prosrc ~ :column AND n.nspname <> ALL (:archives) "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    ), {"column": COLUMN, **archives}).scalars())
    if readers:
        raise RuntimeError(f"refuse: functions still read {COLUMN}: {', '.join(readers)}")
    after = _archive_digests()
    if after != before:
        changed = sorted(t for t in before.keys() | after.keys() if before.get(t) != after.get(t))
        raise RuntimeError(f"refuse: archive content changed in {', '.join(changed)}")


def upgrade() -> None:
    _refuse_other_realms()
    _refuse_other_realms_in_the_read_archive()
    before = _archive_digests()

    for table in LIVE_TABLES:
        op.execute(f"DROP TRIGGER database_realm_write ON {table}")
    for table in LEGACY_ARCHIVE_GUARDED:
        op.execute(f"DROP TRIGGER database_realm_write ON legacy_archive.{table}")

    for table, name, *_ in FOREIGN_KEYS:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
    for table, name in CHECKS_DROPPED:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")

    for name, body in FUNCTIONS.items():
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger\n"
            f"    LANGUAGE plpgsql SET search_path = pg_catalog AS $function${body} $function$"
        )

    for table in LIVE_TABLES:
        op.execute("-- squawk-ignore ban-drop-column\n"
                   f"ALTER TABLE {table} DROP COLUMN {COLUMN}")

    for table, name, columns in PRIMARY_KEYS:
        op.execute("-- squawk-ignore adding-serial-primary-key-field,disallowed-unique-constraint,"
                   "constraint-missing-not-valid\n"
                   f"ALTER TABLE public.{table} ADD CONSTRAINT {name} PRIMARY KEY ({columns})")
    for table, name, columns in UNIQUE_CONSTRAINTS:
        op.execute("-- squawk-ignore disallowed-unique-constraint,constraint-missing-not-valid\n"
                   f"ALTER TABLE public.{table} ADD CONSTRAINT {name} UNIQUE ({columns})")
    for table, name, unique, columns, predicate in INDEXES:
        op.execute(
            "-- squawk-ignore require-concurrent-index-creation\n"
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON public.{table} ({columns})"
            + (f" WHERE {predicate}" if predicate else "")
        )
    op.execute("-- squawk-ignore constraint-missing-not-valid\n"
               "ALTER TABLE public.trading_state ADD CONSTRAINT ck_trading_state_evidence "
               f"CHECK ({TRADING_STATE_EVIDENCE})")

    for table, name, columns, target, target_columns in FOREIGN_KEYS:
        op.execute(
            "-- squawk-ignore constraint-missing-not-valid,adding-foreign-key-constraint\n"
            f"ALTER TABLE public.{table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
            f"REFERENCES public.{target} ({target_columns}) ON DELETE RESTRICT"
        )

    op.execute("-- squawk-ignore ban-drop-function\n"
               "DROP FUNCTION public.guard_database_realm()")
    _assert_end_state(before)


def downgrade() -> None:
    raise RuntimeError(
        "545b2877e9bd is irreversible: the per-row realm values are gone "
        "(docs/adr/2026-10-08-forward-only-migrations.md)")
