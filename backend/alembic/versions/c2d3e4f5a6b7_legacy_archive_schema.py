"""Move the frozen legacy authority's tables into ``legacy_archive``; no role writes them.

S1-8 D4 (Will, 2026-10-06). Since the switch the ledger is the only capital authority and
``e8f9a0b1c2d3`` freezes the twelve tables only the legacy authority wrote. This revision
archives them in place:

* the one live foreign key into them goes first: ``uncertainty_resolution_requests.
  resolved_event_seq`` -> ``event_log`` (``fk_uncertainty_resolution_requests_event``). The
  column stays: pre-switch applied requests name the legacy event their resolution appended
  (history the operator's request list shows), and the outcome-shape CHECK keeps it NULL for
  every ledger request. No other table outside the twelve references them, except the
  equally frozen ``release_archive.canary_command_permits`` (-> ``submission_attempts``),
  which stays: archive to archive;
* ``legacy_archive.manifest`` records, per table, the row count, a SHA-256 over the rows
  (``to_jsonb(row)::text`` sorted, joined by newlines; recompute with ``_digest``) and every
  privilege a role other than the owner held on the table, its columns and its owned
  sequences (``revoked_privileges``), which is what the downgrade grants back;
* the twelve tables move unchanged (``ALTER TABLE ... SET SCHEMA``): rows, columns,
  constraints, indexes, owned sequences, their own triggers, and the foreign keys among them
  and to ``public`` (accounts, execution decisions);
* the epoch-gated, owner-exempt freeze of ``e8f9a0b1c2d3`` (``legacy_authority_write`` ->
  ``public.guard_legacy_authority``, which nothing else uses) is replaced, as in
  ``release_archive``, by ``archive_frozen``: a statement trigger refusing INSERT, UPDATE,
  DELETE and TRUNCATE from every role, the owner included, on the twelve and the manifest
  (``legacy_archive.reject_mutation``). A deliberate owner write disables it explicitly;
* every privilege of every non-owner role (``PUBLIC`` included) on them and their sequences
  is revoked, writes and reads alike; the revision then grants back only what a remaining
  reader needs (``READERS``): the web API's archived execution history
  (``modules.execution.archived_execution_history``) reads exactly ``WEBAPI_EVENT_COLUMNS`` of
  ``event_log``; the switch scaffolding's ``bfx_cutover_reader`` keeps the column reads it
  had (comparison and seed, deleted in PR-D, which revokes them). USAGE on the schema goes
  to those two roles only.

Downgrade refuses without the manifest, drops ``archive_frozen``, revokes every non-owner
privilege again, moves the tables back to ``public``, grants back exactly
``revoked_privileges`` (to roles that still exist), restores ``e8f9a0b1c2d3``'s freeze
function and triggers, drops the manifest and the schema, and restores the foreign key.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""

import json

from sqlalchemy import text

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None
# Rows, cursors and the projection archive are unchanged; only where the frozen tables live.
ledger_contract = "preserved"

SCHEMA = "legacy_archive"
TABLES: tuple[str, ...] = (
    "event_log",
    "event_prefix_hashes",
    "projection_heads",
    "position_state",
    "venue_offer_state",
    "venue_credit_state",
    "reconcile_observation",
    "offer_claims",
    "submission_attempts",
    "execution_uncertainties",
    "capital_snapshot_queries",
    "capital_snapshots",
)
# The web API's exact allowlist (d0e1f2a3b4c6's pattern; this is the newest copy, and
# tests/integration/test_webapi_privilege_allowlist.py compares the effective privileges at head
# with it): in ``public``, f9a0b1c2d3e4's copy without the five archived tables it read
# (``event_log``, ``execution_uncertainties``, ``offer_claims``, ``position_state``,
# ``submission_attempts``); in ``legacy_archive``, ``WEBAPI_ARCHIVE_PRIVILEGES`` only.
_RW = ("DELETE", "INSERT", "SELECT", "UPDATE")
_R = ("SELECT",)
WEBAPI_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "account_config_drafts": _RW,
    "api_keys": _RW,
    "attribution_weekly": _R,
    "capital_authority_epoch": _R,
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "capital_policy_revisions": _R,
    "deployments": _R,
    "exchange_account_credentials": ("INSERT", "SELECT", "UPDATE"),
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "funding_cancel_all_audit": _R,
    "funding_candles": _R,
    "funding_credit_history": _R,
    "funding_interest_payments": _R,
    "funding_trades": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
    "user_configs": _RW,
    "user_profiles": ("INSERT", "SELECT", "UPDATE"),
}
# Unchanged from f9a0b1c2d3e4 (column grants; none on an archived table).
WEBAPI_COLUMN_PRIVILEGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("accepted_capital_basis", "SELECT"): (
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    ),
    ("accepted_capital_basis_attempt", "SELECT"): (
        "basis_id", "attempt_id", "symbol", "classification",
    ),
    ("accepted_capital_basis_credit", "SELECT"): ("basis_id", "symbol"),
    ("accepted_capital_basis_quarantine", "SELECT"): ("basis_id", "quarantine_id"),
    ("accepted_capital_basis_symbol", "SELECT"): (
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    ),
    ("alembic_version", "SELECT"): ("version_num",),
    ("capital_policy_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    ),
    ("execution_resolution_journal", "SELECT"): (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    ),
    ("ledger_observation", "SELECT"): (
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest",
        "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
        "offer_history_complete", "credit_history_complete", "trades_complete",
        "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
        "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
        "trades_requested_start_ms", "trades_requested_end_ms", "history_symbols",
        "first_page_counts",
    ),
    ("ledger_observation_credit_history", "SELECT"): (
        "observation_id", "venue_credit_id", "source_kind", "symbol", "amount", "rate",
        "terminal_kind", "occurred_at_ms",
    ),
    ("ledger_observation_offer", "SELECT"): (
        "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining",
        "rate", "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created",
        "mts_updated",
    ),
    ("ledger_observation_offer_history", "SELECT"): (
        "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining",
        "rate", "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created",
        "mts_updated", "terminal_kind", "occurred_at_ms",
    ),
    ("ledger_observation_query", "SELECT"): (
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    ),
    ("quarantine_opening", "SELECT"): (
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    ),
    ("submission_attempt_journal", "SELECT"): (
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
        "match_rate", "match_period_days", "match_offer_type", "match_flags",
    ),
    ("trading_control_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    ),
    ("transport_outcome_journal", "SELECT"): (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    ),
    ("uncertainty_resolution_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "venue_offer_id", "action", "decision", "reason", "requested_by", "created_at_ms",
        "reconcile_event_seq", "observation_id",
    ),
    ("venue_offer_mirror", "SELECT"): (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    ),
}
WEBAPI = "bfx_webapi"
CUTOVER_READER = "bfx_cutover_reader"
WEBAPI_EVENT_COLUMNS: tuple[str, ...] = (
    "event_seq", "exchange_account_id", "deployment_environment", "event_type",
    "occurred_at_ms", "venue_offer_id", "cid", "payload",
)
WEBAPI_ARCHIVE_PRIVILEGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("event_log", "SELECT"): WEBAPI_EVENT_COLUMNS,
}
# Roles that keep a read after the move: USAGE on the schema, and what ``_grant_readers`` gives.
READERS: tuple[str, ...] = (WEBAPI, CUTOVER_READER)

_FROZEN_TRIGGER = "archive_frozen"
# e8f9a0b1c2d3's freeze, replaced here and restored by the downgrade, verbatim.
_LEGACY_FUNCTION = "guard_legacy_authority"
_LEGACY_TRIGGER = "legacy_authority_write"
_LEGACY_FUNCTION_SQL = f"""CREATE FUNCTION public.{_LEGACY_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID)
             AND (SELECT e.authority FROM public.capital_authority_epoch e
                    ORDER BY e.epoch_seq DESC LIMIT 1) IS NOT DISTINCT FROM 'ledger' THEN
            RAISE EXCEPTION 'legacy write after the authority switch: %', TG_TABLE_NAME;
          END IF;
          RETURN NULL;
        END $$"""

_LIVE_TABLE = "uncertainty_resolution_requests"
_LIVE_FK = "fk_uncertainty_resolution_requests_event"


def _digest(schema: str, table: str) -> str:
    return (f"SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text, "
            f"E'\\n' ORDER BY to_jsonb(t)::text), ''), 'UTF8')), 'hex') FROM {schema}.{table} t")


# Every privilege a non-owner holds on the table, its columns and its owned sequences.
_PRIVILEGES = """
WITH rel AS (SELECT CAST(:qualified AS regclass) AS oid),
objects AS (
  SELECT c.oid, c.relname, c.relkind, c.relowner, c.relacl FROM pg_class c, rel
  WHERE c.oid = rel.oid
  UNION ALL
  SELECT s.oid, s.relname, s.relkind, s.relowner, s.relacl
  FROM pg_depend d JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S', rel
  WHERE d.refobjid = rel.oid AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i'))
SELECT coalesce(jsonb_agg(p ORDER BY p::text), '[]'::jsonb) FROM (
  SELECT o.relname AS object, CASE o.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END AS kind,
         NULL::text AS "column", a.privilege_type AS privilege, a.is_grantable AS grantable,
         CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee
  FROM objects o, aclexplode(o.relacl) a WHERE a.grantee <> o.relowner
  UNION ALL
  SELECT o.relname, 'TABLE', att.attname, a.privilege_type, a.is_grantable,
         CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
  FROM objects o JOIN pg_attribute att ON att.attrelid = o.oid AND att.attnum > 0
       AND NOT att.attisdropped, aclexplode(att.attacl) a
  WHERE o.relkind <> 'S' AND a.grantee <> o.relowner) p
"""


def _role_exists(name: str) -> bool:
    return bool(op.get_bind().execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}).scalar())


def _revoke_all(schema: str) -> None:
    """Revoke every privilege of every non-owner role on the twelve and their sequences, then
    refuse if one survived (a grant only another grantor can take back)."""
    names = ", ".join(f"'{table}'" for table in TABLES)
    op.execute(f"""DO $revoke$ DECLARE o record; g oid; BEGIN
      FOR o IN
        SELECT c.oid, c.relkind, c.relowner FROM pg_class c
        WHERE c.relnamespace = '{schema}'::regnamespace AND c.relname IN ({names})
        UNION ALL
        SELECT s.oid, s.relkind, s.relowner FROM pg_class c
        JOIN pg_depend d ON d.refobjid = c.oid AND d.classid = 'pg_class'::regclass
             AND d.deptype IN ('a', 'i')
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        WHERE c.relnamespace = '{schema}'::regnamespace AND c.relname IN ({names})
      LOOP
        FOR g IN
          SELECT a.grantee FROM aclexplode((SELECT relacl FROM pg_class WHERE oid = o.oid)) a
          WHERE a.grantee <> o.relowner
          UNION
          SELECT a.grantee FROM pg_attribute att, aclexplode(att.attacl) a
          WHERE att.attrelid = o.oid AND a.grantee <> o.relowner
        LOOP
          EXECUTE format('REVOKE ALL ON %s %s FROM %s',
            CASE o.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, o.oid::regclass,
            CASE g WHEN 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(g)) END);
        END LOOP;
        IF EXISTS (
          SELECT 1 FROM aclexplode((SELECT relacl FROM pg_class WHERE oid = o.oid)) a
          WHERE a.grantee <> o.relowner
          UNION ALL
          SELECT 1 FROM pg_attribute att, aclexplode(att.attacl) a
          WHERE att.attrelid = o.oid AND a.grantee <> o.relowner) THEN
          RAISE EXCEPTION 'a non-owner privilege on % survived the revoke', o.oid::regclass;
        END IF;
      END LOOP;
    END $revoke$""")


def _grant_readers() -> None:
    if _role_exists(WEBAPI):
        op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {WEBAPI}")
        for (table, privilege), columns in WEBAPI_ARCHIVE_PRIVILEGES.items():
            op.execute(f"GRANT {privilege} ({', '.join(columns)}) ON {SCHEMA}.{table} TO {WEBAPI}")
    if _role_exists(CUTOVER_READER):
        op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {CUTOVER_READER}")
        # The column reads it had (recorded in the manifest), nothing else.
        op.execute(f"""DO $grant$ DECLARE p record; BEGIN
          FOR p IN SELECT m.table_name, e ->> 'column' AS col
            FROM {SCHEMA}.manifest m, jsonb_array_elements(m.revoked_privileges) e
            WHERE e ->> 'grantee' = '{CUTOVER_READER}' AND e ->> 'privilege' = 'SELECT'
              AND e ->> 'kind' = 'TABLE' AND e ->> 'object' = m.table_name
          LOOP
            EXECUTE format('GRANT SELECT %s ON {SCHEMA}.%I TO {CUTOVER_READER}',
              CASE WHEN p.col IS NULL THEN '' ELSE format('(%I)', p.col) END, p.table_name);
          END LOOP;
        END $grant$""")


def upgrade() -> None:
    op.execute(f"ALTER TABLE public.{_LIVE_TABLE} DROP CONSTRAINT {_LIVE_FK}")

    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"""CREATE TABLE {SCHEMA}.manifest (
        table_name text PRIMARY KEY,
        row_count bigint NOT NULL CHECK (row_count >= 0),
        content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
        revoked_privileges jsonb NOT NULL CHECK (jsonb_typeof(revoked_privileges) = 'array'),
        archived_at timestamptz NOT NULL DEFAULT now(),
        archived_by_revision text NOT NULL)""")
    op.execute(f"REVOKE ALL ON {SCHEMA}.manifest FROM PUBLIC")
    bind = op.get_bind()
    for table in TABLES:
        privileges = bind.execute(text(_PRIVILEGES), {"qualified": f"public.{table}"}).scalar()
        bind.execute(text(
            f"INSERT INTO {SCHEMA}.manifest (table_name, row_count, content_sha256, "
            f"revoked_privileges, archived_by_revision) "
            f"SELECT :table, d.count, d.encode, CAST(:privileges AS jsonb), :revision "
            f"FROM ({_digest('public', table)}) AS d"
        ), {"table": table, "privileges": json.dumps(privileges), "revision": revision})

    for table in TABLES:
        op.execute(f"DROP TRIGGER {_LEGACY_TRIGGER} ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA {SCHEMA}")
    op.execute(f"DROP FUNCTION public.{_LEGACY_FUNCTION}()")
    _revoke_all(SCHEMA)
    _grant_readers()

    op.execute(f"""CREATE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'legacy_archive is frozen: % on %', TG_OP, TG_TABLE_NAME;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_mutation() FROM PUBLIC")
    # Statement-level, so even a write that would touch no row is refused.
    for table in (*TABLES, "manifest"):
        op.execute(f"CREATE TRIGGER {_FROZEN_TRIGGER} BEFORE INSERT OR UPDATE OR DELETE OR "
                   f"TRUNCATE ON {SCHEMA}.{table} FOR EACH STATEMENT "
                   f"EXECUTE FUNCTION {SCHEMA}.reject_mutation()")


def downgrade() -> None:
    bind = op.get_bind()
    recorded = bind.execute(text(
        f"SELECT table_name, revoked_privileges FROM {SCHEMA}.manifest ORDER BY table_name"
    )).all() if bind.execute(text(f"SELECT to_regclass('{SCHEMA}.manifest')")).scalar() else []
    if sorted(row.table_name for row in recorded) != sorted(TABLES):
        raise RuntimeError(f"refuse downgrade: {SCHEMA}.manifest does not list the twelve tables")

    for table in (*TABLES, "manifest"):
        op.execute(f"DROP TRIGGER {_FROZEN_TRIGGER} ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_mutation()")
    _revoke_all(SCHEMA)
    for role in READERS:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {role}")
    for table in TABLES:
        op.execute(f"ALTER TABLE {SCHEMA}.{table} SET SCHEMA public")
    op.execute(_LEGACY_FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION public.{_LEGACY_FUNCTION}() FROM PUBLIC")
    for table in TABLES:
        op.execute(f"CREATE TRIGGER {_LEGACY_TRIGGER} BEFORE INSERT OR UPDATE OR DELETE ON "
                   f"public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.{_LEGACY_FUNCTION}()")
    roles = set(bind.execute(text("SELECT rolname FROM pg_roles")).scalars())
    for row in recorded:
        for entry in row.revoked_privileges:
            grantee = entry["grantee"]
            if grantee != "PUBLIC" and grantee not in roles:
                continue  # a role dropped since holds nothing to give back
            target = "PUBLIC" if grantee == "PUBLIC" else f'"{grantee}"'
            column = f' ("{entry["column"]}")' if entry["column"] is not None else ""
            option = " WITH GRANT OPTION" if entry["grantable"] else ""
            op.execute(f'GRANT {entry["privilege"]}{column} ON {entry["kind"]} '
                       f'public."{entry["object"]}" TO {target}{option}')

    op.execute(f"DROP TABLE {SCHEMA}.manifest")
    op.execute(f"DROP SCHEMA {SCHEMA}")
    op.execute(
        f"ALTER TABLE public.{_LIVE_TABLE} ADD CONSTRAINT {_LIVE_FK} FOREIGN KEY "
        "(resolved_event_seq) REFERENCES public.event_log (event_seq) ON DELETE RESTRICT"
    )
