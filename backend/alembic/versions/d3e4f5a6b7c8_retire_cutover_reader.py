"""Retire the cutover reader: revoke what ``bfx_cutover_reader`` reads, then drop the group.

S1-8 PR-D. The NOLOGIN group ``bfx_cutover_reader`` (created by ``b1e2d3a4c5f6``) existed for
the switch scaffolding only: the capital comparison, the closure verifier and the comparison
rehearsal ran under ``SET LOCAL ROLE`` to it. The switch ran on 2026-10-05 and PR-D deletes that
code, so nothing reads as the group any more. This revision:

* refuses a group that can log in, owns an object, or holds a privilege on anything but
  relations, columns and schemas (or on this database itself): none of that is a migration's;
* revokes every relation, column and schema privilege it holds here, and any default privilege
  naming it, and checks that nothing is left in this database. What the migrations gave it is
  USAGE on ``public`` and ``legacy_archive``, the column SELECTs of ``READER_PUBLIC_COLUMNS``
  in ``public`` (the state at ``c2d3e4f5a6b7``, pinned by ``test_cutover_reader_retirement``),
  and in ``legacy_archive`` the SELECTs ``c2d3e4f5a6b7`` granted back from its manifest; a
  production database holds exactly that (the S1-8 PR-D precheck). Anything more is an
  environment's own grant (the test suite's worst-case builds hand every new table to every
  role by default privileges): revoked too, and not given back;
* drops the group, unless another database of the cluster still holds a privilege for it (a
  production cluster has one application database; the test clusters have many). Dropping it
  also ends every membership in it: the operator's LOGIN ``bfx_cutover_attest`` stays, with no
  membership, and is dropped by the operator (docs/runbooks/fresh-host-setup.md §1c-1).

Downgrade recreates the group as ``b1e2d3a4c5f6`` does (NOLOGIN, its marker comment, so that
revision's own downgrade still drops it) when it is absent, and grants back exactly what the
migrations gave it (above). The operator's LOGIN membership is not restored (it was never a
migration's grant).

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""

from sqlalchemy import text

from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None
# Roles only: no ledger table, cursor or archive changes.
ledger_contract = "preserved"

READER = "bfx_cutover_reader"
ARCHIVE = "legacy_archive"
SCHEMAS: tuple[str, ...] = ("public", ARCHIVE)
# Every column SELECT the migrations gave the group in ``public`` up to ``c2d3e4f5a6b7``
# (b1e2d3a4c5f6 and the ledger migrations after it, b5c6d7e8f9a0, d7e8f9a0b1c2), written out:
# the downgrade grants exactly these back.
READER_PUBLIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "accepted_capital_basis": (
        "accept_revision", "accepted", "accepted_at_ms", "attempt_seq_high_water",
        "deployment_environment", "digest", "exchange_account_id", "id", "observation_id",
        "schema_version", "scope_block",
    ),
    "accepted_capital_basis_attempt": ("attempt_id", "basis_id", "classification", "symbol"),
    "accepted_capital_basis_cell": ("amount", "basis_id", "cell_id", "symbol"),
    "accepted_capital_basis_credit": (
        "amount", "attribution_basis", "basis_id", "mts_opening", "period_days", "source_kind",
        "symbol", "venue_credit_id",
    ),
    "accepted_capital_basis_credit_cell": ("basis_id", "cell_id", "source_kind", "venue_credit_id"),
    "accepted_capital_basis_quarantine": ("basis_id", "quarantine_id"),
    "accepted_capital_basis_symbol": (
        "available", "basis_id", "block", "conservation", "credits", "fill_conflicts",
        "foreign_executed", "foreign_offers", "lent_unexplained", "offered", "symbol",
        "unattributed_credits",
    ),
    "capital_authority_epoch": (
        "actor", "authority", "epoch_seq", "evidence", "reason", "set_at_ms",
    ),
    "capital_command_clock": ("deployment_environment", "exchange_account_id", "revision"),
    "capital_policy_heads": (
        "deployment_environment", "exchange_account_id", "revision", "revision_id", "symbol",
    ),
    "capital_policy_requests": (
        "deployment_environment", "exchange_account_id", "request_id", "state",
    ),
    "capital_policy_revisions": (
        "deployment_environment", "digest", "exchange_account_id", "id", "policy", "revision",
        "schema_version", "source", "symbol",
    ),
    "execution_decisions": (
        "account_id", "amount_usdt", "applied_rate", "cell_id", "config_hash", "decision_id",
        "deployment_environment", "duration_days", "exchange_account_id", "execution_policy",
        "failed_dependency", "model_evidence", "model_hash", "model_version", "occurred_at_ms",
        "outcome", "reason_code", "reconcile_id", "recorded_at_ms", "safety_result",
        "service_version", "signal_correlation_id", "signal_rate", "snapshot_age_ms",
        "snapshot_captured_at_ms", "snapshot_hash", "snapshot_id", "snapshot_source", "strategy",
        "symbol",
    ),
    "execution_resolution_journal": (
        "action", "actor_id", "actor_kind", "attempt_id", "candidate_count",
        "deployment_environment", "exchange_account_id", "id", "observation_id",
        "operator_request_id", "quarantine_id", "reason", "resolved_at_ms", "symbol",
        "venue_offer_id",
    ),
    "funding_trades": (
        "amount", "deployment_environment", "exchange_account_id", "maker", "mts_create",
        "offer_id", "period", "rate", "recorded_at", "symbol", "trade_id",
    ),
    "ledger_observation": (
        "accept_revision", "accepted", "confirmation_digest", "confirmation_finished_at_ms",
        "credit_history_complete", "credit_history_pages", "credits_complete",
        "deployment_environment", "exchange_account_id", "first_digest",
        "history_newest_mts_created", "history_oldest_mts_created", "history_requested_end_ms",
        "history_requested_start_ms", "id", "loans_complete", "offer_history_complete",
        "offer_history_pages", "offers_complete", "origin", "query_finished_at_ms", "query_id",
        "schema_version", "trades_complete", "trades_requested_end_ms", "trades_requested_start_ms",
        "wallets_complete",
    ),
    "ledger_observation_credit": (
        "amount", "flags", "id", "mts_created", "mts_opening", "mts_updated", "observation_id",
        "period_days", "rate", "source_kind", "status", "symbol", "venue_credit_id",
    ),
    "ledger_observation_credit_history": (
        "amount", "flags", "id", "mts_created", "mts_opening", "mts_updated", "observation_id",
        "occurred_at_ms", "period_days", "rate", "source_kind", "status", "symbol", "terminal_kind",
        "venue_credit_id",
    ),
    "ledger_observation_offer": (
        "amount_original", "amount_remaining", "flags", "id", "mts_created", "mts_updated",
        "observation_id", "offer_type", "period_days", "rate", "rate_observed", "status", "symbol",
        "venue_offer_id",
    ),
    "ledger_observation_offer_history": (
        "amount_original", "amount_remaining", "flags", "id", "mts_created", "mts_updated",
        "observation_id", "occurred_at_ms", "offer_type", "period_days", "rate", "rate_observed",
        "status", "symbol", "terminal_kind", "venue_offer_id",
    ),
    "ledger_observation_query": (
        "deployment_environment", "exchange_account_id", "query_id", "query_revision",
        "start_revision", "started_at_ms",
    ),
    "ledger_observation_trade": (
        "amount", "maker", "mts_create", "observation_id", "period_days", "rate", "symbol",
        "trade_id", "venue_offer_id",
    ),
    "ledger_observation_wallet": (
        "available", "balance", "currency", "observation_id", "symbol", "wallet_type",
    ),
    "quarantine_member": (
        "amount_at_join", "observation_id", "quarantine_id", "source_kind", "venue_object_id",
    ),
    "quarantine_opening": (
        "deployment_environment", "exchange_account_id", "intended_amount",
        "legacy_reconcile_event_seq", "opened_at_ms", "opened_revision", "quarantine_id",
        "source_attempt_id", "symbol",
    ),
    "submission_attempt_journal": (
        "attempt_id", "attempt_seq", "basis_id", "cell_id", "deployment_environment",
        "exchange_account_id", "execution_decision_id", "intended_amount", "normalized_payload",
        "payload_sha256", "policy_revision_id", "seed_provenance", "started_at_ms", "symbol",
    ),
    "trading_control_requests": (
        "deployment_environment", "exchange_account_id", "request_id", "state",
    ),
    "trading_state": ("deployment_environment", "exchange_account_id", "id"),
    "transport_outcome_journal": (
        "attempt_id", "completed_at_ms", "kind", "reason", "venue_offer_id",
    ),
    "uncertainty_resolution_requests": (
        "deployment_environment", "exchange_account_id", "outcome_reason", "request_id", "state",
    ),
    "venue_credit_mirror": (
        "amount", "deployment_environment", "exchange_account_id", "flags",
        "last_accepted_observation_id", "mts_created", "mts_opening", "mts_updated", "period_days",
        "present_in_latest_accepted_snapshot", "rate", "source_kind", "status", "symbol",
        "terminal_evidence_id", "terminal_kind", "venue_credit_id",
    ),
    "venue_offer_mirror": (
        "amount_original", "amount_remaining", "deployment_environment", "exchange_account_id",
        "flags", "last_accepted_observation_id", "mts_created", "mts_updated", "offer_type",
        "period_days", "present_in_latest_accepted_snapshot", "rate", "rate_observed", "status",
        "symbol", "terminal_evidence_id", "terminal_kind", "venue_offer_id",
    ),
}

# The column SELECTs c2d3e4f5a6b7 granted back in ``legacy_archive``: what its manifest
# recorded the group held on the twelve tables before the move (its ``_grant_readers``).
_ARCHIVE_GRANTS = f"""
SELECT m.table_name, e ->> 'column' AS column_name
FROM {ARCHIVE}.manifest m, jsonb_array_elements(m.revoked_privileges) e
WHERE e ->> 'grantee' = '{READER}' AND e ->> 'privilege' = 'SELECT'
  AND e ->> 'kind' = 'TABLE' AND e ->> 'object' = m.table_name
"""

# Every privilege the group holds in this database, one row each:
# (kind, schema, object, column, privilege, grantable).
_HELD = f"""
WITH reader AS (SELECT oid FROM pg_roles WHERE rolname = '{READER}')
SELECT CASE c.relkind WHEN 'S' THEN 'sequence' ELSE 'table' END, n.nspname, c.relname, NULL,
       a.privilege_type, a.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a, reader
WHERE a.grantee = reader.oid
UNION ALL
SELECT 'column', n.nspname, c.relname, att.attname, a.privilege_type, a.is_grantable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute att ON att.attrelid = c.oid AND att.attnum > 0 AND NOT att.attisdropped,
     aclexplode(att.attacl) a, reader
WHERE a.grantee = reader.oid
UNION ALL
SELECT 'schema', n.nspname, NULL, NULL, a.privilege_type, a.is_grantable
FROM pg_namespace n, aclexplode(n.nspacl) a, reader WHERE a.grantee = reader.oid
"""

# Dependencies the rows above do not cover: anything else in this database (functions, types,
# ownership) and this database's own ACL. Must be empty. Default privileges are not counted:
# see _DEFAULTS.
_OTHER = f"""
WITH reader AS (SELECT oid FROM pg_roles WHERE rolname = '{READER}'),
here AS (SELECT oid FROM pg_database WHERE datname = current_database())
SELECT d.classid::regclass::text, d.deptype::text FROM pg_shdepend d, reader, here
WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = reader.oid
  AND ((d.dbid = here.oid AND (d.deptype <> 'a' OR d.classid NOT IN
        ('pg_class'::regclass, 'pg_namespace'::regclass, 'pg_default_acl'::regclass)))
       OR (d.dbid = 0 AND d.classid = 'pg_database'::regclass AND d.objid = here.oid))
"""

# Default privileges naming the group (``ALTER DEFAULT PRIVILEGES ... TO bfx_cutover_reader``):
# an environment's own rule for future objects, never a migration's grant, and none in
# production (fresh-host-setup §1a gives them to bfx_bot only). They would keep the group from
# being dropped, so they are revoked; there is no record of them to give back.
_DEFAULTS = f"""
SELECT pg_get_userbyid(d.defaclrole), d.defaclnamespace::regnamespace::text, d.defaclobjtype
FROM pg_default_acl d, aclexplode(d.defaclacl) a
WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '{READER}')
GROUP BY 1, 2, 3
"""
_DEFAULT_OBJECTS = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES",
                    "n": "SCHEMAS", "L": "LARGE OBJECTS"}

# What keeps the group from being dropped: a dependency in another database of the cluster.
_ELSEWHERE = f"""
SELECT count(*) FROM pg_shdepend d
WHERE d.refclassid = 'pg_authid'::regclass
  AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = '{READER}')
"""


def _reader_exists() -> bool:
    return bool(op.get_bind().execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER}).scalar())


def _held() -> set[tuple[object, ...]]:
    return {tuple(row) for row in op.get_bind().execute(text(_HELD)).all()}


def upgrade() -> None:
    if not _reader_exists():
        return
    bind = op.get_bind()
    if bind.execute(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"),
                    {"r": READER}).scalar():
        raise RuntimeError(f"refuse: {READER} must be NOLOGIN")
    other = bind.execute(text(_OTHER)).all()
    if other:
        raise RuntimeError(f"refuse: {READER} has {len(other)} dependencies besides its "
                           "relation, column, schema and default privileges in this database")

    for kind, schema, table, column, privilege, _ in sorted(_held(), key=repr):
        if kind == "schema":
            op.execute(f'REVOKE {privilege} ON SCHEMA "{schema}" FROM {READER}')
        elif kind in {"table", "sequence"}:
            op.execute(f'REVOKE {privilege} ON {kind.upper()} "{schema}"."{table}" FROM {READER}')
        else:
            op.execute(f'REVOKE {privilege} ("{column}") ON "{schema}"."{table}" FROM {READER}')
    for grantor, schema, objtype in bind.execute(text(_DEFAULTS)).all():
        scoped = "" if schema == "-" else f' IN SCHEMA "{schema}"'
        op.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{grantor}"{scoped} '
                   f"REVOKE ALL ON {_DEFAULT_OBJECTS[objtype]} FROM {READER}")
    if _held() or bind.execute(text(_OTHER)).all() or bind.execute(text(_DEFAULTS)).all():
        raise RuntimeError(f"refuse: a privilege of {READER} survived the revoke")
    if bind.execute(text(_ELSEWHERE)).scalar() == 0:
        op.execute(f"DROP ROLE {READER}")
    # Otherwise another database of this cluster still grants it something: the group stays,
    # holding nothing here (a test cluster's other databases; never production's one).


def downgrade() -> None:
    bind = op.get_bind()
    if not _reader_exists():
        # As b1e2d3a4c5f6 creates it, marker included, so its downgrade still drops it.
        database = bind.execute(text("SELECT current_database()")).scalar_one()
        op.execute(f"CREATE ROLE {READER} NOLOGIN")
        marker = f"created by b1e2d3a4c5f6 in {database}".replace("'", "''")
        op.execute(f"COMMENT ON ROLE {READER} IS '{marker}'")
    elif bind.execute(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"),
                      {"r": READER}).scalar():
        raise RuntimeError(f"refuse: {READER} must be NOLOGIN")
    for schema in SCHEMAS:
        op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {READER}')
    for table, columns in READER_PUBLIC_COLUMNS.items():
        listed = ", ".join(f'"{column}"' for column in columns)
        op.execute(f'GRANT SELECT ({listed}) ON public."{table}" TO {READER}')
    archived: dict[str, list[str]] = {}
    for table, column in bind.execute(text(_ARCHIVE_GRANTS)).all():
        if column is None:
            op.execute(f'GRANT SELECT ON {ARCHIVE}."{table}" TO {READER}')
        else:
            archived.setdefault(table, []).append(column)
    for table, columns in sorted(archived.items()):
        listed = ", ".join(f'"{column}"' for column in sorted(columns))
        op.execute(f'GRANT SELECT ({listed}) ON {ARCHIVE}."{table}" TO {READER}')
