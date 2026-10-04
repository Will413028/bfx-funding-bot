"""Column-level SELECT for the cutover reader on what the capital comparison reads.

The reader runs the comparison (``apps/capital_comparison``) under ``SET ROLE``:
the legacy baseline and candidate loader read the legacy authority tables, the
ledger capital reader reads the ledger by named columns, and the reverse scope
inventory reads the scope columns of every table that can hold authority state.
This revision grants exactly those columns, never a table, so the attestation
"group table grants 0" still holds. ``evidence``, ``normalized_payload``,
``raw`` and ``authorization_evidence`` stay denied.

* ``_LEGACY_COLUMNS``: every mapped column of the ten legacy tables. The baseline
  loads whole ORM rows of them (``capital_shadow_baseline`` / ``capital_repository``)
  and the loader names a subset, so the union is the full row.
* ``_LEDGER_COLUMNS``: the verdict columns ``capital_reader`` reads (an earlier
  revision held them back while no consumer read them outside the bot) and the
  generated ``intended_amount`` it reads instead of the payload.
* ``_INVENTORY_COLUMNS``: scope (and state) columns of the tables the inventory
  scans that the loader and baseline do not read.

Data-free: privileges only. The downgrade revokes exactly these columns.

Revision ID: b5c6d7e8f9a0
Revises: a7c3e9f1b2d4
"""

from sqlalchemy import text

from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_READER = "bfx_cutover_reader"

_LEGACY_COLUMNS = {
    "event_log": (
        "event_seq",
        "account_id",
        "exchange_account_id",
        "deployment_environment",
        "event_type",
        "cid",
        "venue_offer_id",
        "venue_seq",
        "event_id",
        "schema_version",
        "payload",
        "occurred_at_ms",
        "recorded_at",
    ),
    "event_prefix_hashes": (
        "event_seq",
        "exchange_account_id",
        "deployment_environment",
        "prefix_hash",
    ),
    "capital_policy_heads": (
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "revision_id",
        "revision",
    ),
    "capital_policy_revisions": (
        "id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "revision",
        "schema_version",
        "policy",
        "digest",
        "source",
    ),
    "capital_snapshots": (
        "event_seq",
        "query_id",
        "exchange_account_id",
        "deployment_environment",
        "schema_version",
        "command_fence",
        "classification",
        "covered_prefix_hash",
        "authorization_blocked_reason",
    ),
    "capital_snapshot_queries": (
        "id",
        "exchange_account_id",
        "deployment_environment",
        "command_fence",
        "query_revision",
        "started_at_ms",
    ),
    "execution_decisions": (
        "decision_id",
        "account_id",
        "exchange_account_id",
        "deployment_environment",
        "reconcile_id",
        "cell_id",
        "strategy",
        "symbol",
        "signal_correlation_id",
        "outcome",
        "reason_code",
        "failed_dependency",
        "signal_rate",
        "applied_rate",
        "amount_usdt",
        "duration_days",
        "snapshot_id",
        "snapshot_hash",
        "snapshot_captured_at_ms",
        "snapshot_source",
        "snapshot_age_ms",
        "model_version",
        "model_hash",
        "model_evidence",
        "safety_result",
        "execution_policy",
        "service_version",
        "config_hash",
        "occurred_at_ms",
        "recorded_at_ms",
    ),
    "projection_heads": (
        "exchange_account_id",
        "deployment_environment",
        "projection_name",
        "last_event_seq",
        "projector_version",
        "updated_at",
    ),
    "submission_attempts": (
        "attempt_id",
        "execution_decision_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "cid",
        "normalized_payload",
        "payload_sha256",
        "started_at_ms",
        "completed_at_ms",
        "outcome_kind",
        "outcome_reason",
        "venue_offer_id",
        "last_event_seq",
        "recorded_at",
    ),
    "execution_uncertainties": (
        "uncertainty_id",
        "exchange_account_id",
        "deployment_environment",
        "symbol",
        "kind",
        "correlation_key",
        "state",
        "intended_amount",
        "evidence",
        "attempt_id",
        "venue_offer_id",
        "opened_event_seq",
        "reconcile_event_seq",
        "resolved_event_seq",
        "resolved_by_operator_id",
        "resolution_reason",
        "resolution_evidence",
        "opened_at",
        "resolved_at",
    ),
}

_LEDGER_COLUMNS = {
    "accepted_capital_basis_symbol": (
        "conservation",
        "lent_unexplained",
        "foreign_executed",
        "fill_conflicts",
    ),
    "submission_attempt_journal": ("intended_amount",),
}

_INVENTORY_COLUMNS = {
    "offer_claims": ("exchange_account_id", "deployment_environment", "state", "symbol"),
    "trading_state": ("exchange_account_id", "deployment_environment"),
    "uncertainty_resolution_requests": (
        "exchange_account_id",
        "deployment_environment",
        "state",
    ),
    "capital_policy_requests": ("exchange_account_id", "deployment_environment", "state"),
    "trading_control_requests": ("exchange_account_id", "deployment_environment", "state"),
}

_GRANTS = {**_LEGACY_COLUMNS, **_LEDGER_COLUMNS, **_INVENTORY_COLUMNS}


def _reader_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": _READER})
        .scalar()
    )


def upgrade() -> None:
    if not _reader_exists():
        return
    for table, columns in _GRANTS.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON public.{table} TO {_READER}")


def downgrade() -> None:
    if not _reader_exists():
        return
    for table, columns in _GRANTS.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON public.{table} FROM {_READER}")
