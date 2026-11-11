"""Narrow twins of every key that carries ``deployment_environment``; the guard fills it in.

A database holds one realm (``database_realm``, a7c3e9f1b2d4), so on every live table the
column repeats the stamp on each row. This is the additive first step of removing it
(docs/plans/2026-11-08-drop-realm-columns.md, step 1); nothing is dropped here and code that
still writes the column behaves as before.

* Assert it: a live table (any schema but the two archives) holding a row whose realm is not
  the stamp refuses the upgrade; so does any row at all in an unstamped database. The tables
  come from ``pg_catalog``, the probe is one statement (a ``UNION ALL`` of one ``EXISTS`` per
  table), so the answer is from a single snapshot. The message names tables, never data.
* ``guard_database_realm()`` fills a NULL ``deployment_environment`` with the stamp before it
  compares. PostgreSQL checks NOT NULL after the BEFORE ROW triggers, so an INSERT may now
  omit the column. An unstamped database and a realm other than the stamp are refused as
  before. ``CREATE OR REPLACE`` keeps the owner, SECURITY DEFINER and the revoked EXECUTE.
* For each primary key, unique constraint and index that includes the column, an index over
  the same columns without it (same order, direction, uniqueness and predicate), named
  ``<original>_narrow``. Step 3 swaps them in under the original names. None of the 38
  duplicates an existing index.

The downgrade loses nothing (it drops the twins and restores the guard that does not fill), so
it is written (docs/adr/2026-10-08-forward-only-migrations.md D1); fixtures that reach an older
revision by downgrading from head pass through it.

A narrow unique index that cannot be built means two rows differ only by realm, which the
assertion above has already refused.

squawk's ``require-concurrent-index-creation`` guards against blocking writers on a live table;
bfx-deploy has stopped the bot and the webapi (the only writers) while migrations run, and
CONCURRENTLY cannot run inside the revision's transaction (precedent: a9d2c4e6f813).

Revision ID: db388a84678f
Revises: 96535191e443
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "db388a84678f"
down_revision = "96535191e443"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_ARCHIVE_SCHEMAS = ("legacy_archive", "release_archive")

# One row per live table that holds a row of another realm than the stamp. Without a stamp the
# scalar subquery is NULL and every row is distinct from it.
_PROBE = """
SELECT string_agg(
  format('SELECT %L AS t WHERE EXISTS (SELECT 1 FROM %I.%I WHERE deployment_environment '
         'IS DISTINCT FROM (SELECT realm FROM public.database_realm))',
         format('%I.%I', n.nspname, c.relname), n.nspname, c.relname),
  ' UNION ALL ' ORDER BY n.nspname, c.relname)
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE a.attname = 'deployment_environment' AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname <> ALL (:archives)
"""

_FILL = """      IF NEW.deployment_environment IS NULL THEN
        NEW.deployment_environment := stamp;
      END IF;
"""
_GUARD = """CREATE OR REPLACE FUNCTION public.guard_database_realm() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
    DECLARE stamp text;
    BEGIN
      SELECT r.realm INTO stamp FROM public.database_realm r;
      IF stamp IS NULL THEN
        RAISE EXCEPTION 'database realm is not stamped: refusing a write to %', TG_TABLE_NAME;
      END IF;
{fill}      IF NEW.deployment_environment IS DISTINCT FROM stamp THEN
        RAISE EXCEPTION 'database realm % refuses a write of realm % to %',
          stamp, NEW.deployment_environment, TG_TABLE_NAME;
      END IF;
      RETURN NEW;
    END $$""".replace("{fill}", _FILL)

_REQUESTED = "state = 'requested'"
# (table, the wide key or index it twins, unique, columns, predicate). The index is named
# ``<wide>_narrow``; the columns are the wide ones minus ``deployment_environment``.
NARROW_INDEXES: tuple[tuple[str, str, bool, str, str | None], ...] = (
    ("accepted_capital_basis", "ix_accepted_capital_basis_scope_accepted", False,
     "exchange_account_id, accepted_at_ms DESC", None),
    ("attribution_legacy_offer_cells", "attribution_legacy_offer_cells_pkey", True,
     "exchange_account_id, venue_offer_id, cell", None),
    ("attribution_weekly", "attribution_weekly_pkey", True,
     "exchange_account_id, cell, week_start_ms", None),
    ("bot_runs", "ix_bot_runs_scope_started", False, "exchange_account_id, started_at_ms", None),
    ("capital_command_clock", "capital_command_clock_pkey", True, "exchange_account_id", None),
    ("capital_policy_heads", "capital_policy_heads_pkey", True,
     "exchange_account_id, symbol", None),
    ("capital_policy_requests", "uq_capital_policy_requests_scope", True,
     "request_id, exchange_account_id, symbol", None),
    ("capital_policy_requests", "ix_capital_policy_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("capital_policy_requests", "uq_capital_policy_requests_pending", True,
     "exchange_account_id, symbol, action", _REQUESTED),
    ("capital_policy_revisions", "uq_capital_policy_revision_scope", True,
     "exchange_account_id, symbol, revision", None),
    ("config_regime", "config_regime_pkey", True, "exchange_account_id, recorded_at_ms", None),
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
    ("ledger_observation_query", "uq_ledger_observation_query_scope_revision", True,
     "exchange_account_id, query_revision", None),
    ("ledger_observation_query", "ix_ledger_observation_query_scope_revision", False,
     "exchange_account_id, query_revision DESC", None),
    ("nav_peak", "nav_peak_pkey", True, "exchange_account_id, symbol", None),
    ("nav_window_samples", "ix_nav_window_samples_scope_symbol_time", False,
     "exchange_account_id, symbol, occurred_at_ms", None),
    ("quarantine_opening", "ix_quarantine_opening_scope_revision", False,
     "exchange_account_id, opened_revision", None),
    ("sim_venue_event", "pk_sim_venue_event", True, "exchange_account_id, seq", None),
    ("submission_attempt_journal", "uq_submission_attempt_scope_seq", True,
     "exchange_account_id, attempt_seq", None),
    ("trading_control_requests", "uq_trading_control_requests_scope", True,
     "request_id, exchange_account_id", None),
    ("trading_control_requests", "ix_trading_control_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("trading_control_requests", "uq_trading_control_requests_pending", True,
     "exchange_account_id", f"{_REQUESTED} AND action <> 'kill'"),
    ("trading_control_requests", "uq_trading_control_requests_pending_kill", True,
     "exchange_account_id", f"{_REQUESTED} AND action = 'kill'"),
    ("trading_state", "ix_trading_state_scope_id", True, "exchange_account_id, id", None),
    ("uncertainty_resolution_requests", "uq_uncertainty_resolution_requests_scope", True,
     "request_id, exchange_account_id, uncertainty_id", None),
    ("uncertainty_resolution_requests", "ix_uncertainty_resolution_requests_queue", False,
     "exchange_account_id, state, created_at_ms", None),
    ("uncertainty_resolution_requests", "uq_uncertainty_resolution_requests_pending", True,
     "exchange_account_id, uncertainty_id", _REQUESTED),
    ("venue_credit_mirror", "venue_credit_mirror_pkey", True,
     "exchange_account_id, venue_credit_id, source_kind", None),
    ("venue_credit_mirror", "ix_venue_credit_mirror_live", False,
     "exchange_account_id, symbol", "present_in_latest_accepted_snapshot"),
    ("venue_offer_mirror", "venue_offer_mirror_pkey", True,
     "exchange_account_id, venue_offer_id", None),
    ("venue_offer_mirror", "ix_venue_offer_mirror_live", False,
     "exchange_account_id, symbol", "present_in_latest_accepted_snapshot"),
)


def _refuse_other_realms() -> None:
    bind = op.get_bind()
    probe = bind.execute(text(_PROBE), {"archives": list(_ARCHIVE_SCHEMAS)}).scalar()
    if not probe:
        return
    holding = list(bind.exec_driver_sql(str(probe)).scalars())
    if holding:
        raise RuntimeError(
            "refuse: rows of a realm other than the database_realm stamp in "
            + ", ".join(holding))


def upgrade() -> None:
    _refuse_other_realms()
    op.execute(_GUARD)
    for table, wide, unique, columns, predicate in NARROW_INDEXES:
        op.execute(
            "-- squawk-ignore require-concurrent-index-creation\n"
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {wide}_narrow ON public.{table} ({columns})"
            + (f" WHERE {predicate}" if predicate else "")
        )


def downgrade() -> None:
    for _table, wide, _unique, _columns, _predicate in NARROW_INDEXES:
        op.execute(
            "-- squawk-ignore require-concurrent-index-deletion\n"
            f"DROP INDEX public.{wide}_narrow"
        )
    op.execute(_GUARD.replace(_FILL, ""))
