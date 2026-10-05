"""Column-level SELECT for the cutover reader on what the cutover arms read (S1-4e).

The cutover comparison (``apps/capital_comparison`` cutover mode) runs as the reader under
``SET ROLE``. Its two new arms read columns earlier grants (``b5c6d7e8f9a0`` and before) held
back. This revision grants exactly those columns, never a table, so the attestation "group table
grants 0" still holds:

* legacy arm (F3 (i'), ``execution.capital_observed_baseline``): ``CapitalRepository._classify``
  and the historical-intent proof load whole ``offer_claims`` and ``funding_trades`` ORM rows, so
  every column of both (``offer_claims``' scope and state columns were granted for the inventory);
* closure verifier (``apps/capital_comparison_closure``):
  - ``submission_attempt_journal.seed_provenance`` (every seeded attempt resolves to legacy) and
    ``normalized_payload`` (the ledger's own ``fingerprints_in_use`` reads the submitted amount);
  - ``ledger_observation.origin`` (which observation is the seed's);
  - ``trading_state.id`` (the latest row per scope is unchanged since the seed);
  - ``uncertainty_resolution_requests.request_id`` / ``outcome_reason`` and the ``request_id`` of
    ``capital_policy_requests`` and ``trading_control_requests`` (failed and carried requests);
  - ``venue_offer_state`` / ``venue_credit_state`` scope, ``symbol`` and ``is_terminal`` (symbols
    the legacy projections hold live need a cell, S1-4a R1-5).

Still denied: ``authorization_evidence``, every ``evidence`` and ``raw`` column, policy ``source``
stays as granted before. Data-free: privileges only. The downgrade revokes exactly these columns
(none of them was granted before this revision).

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""

from sqlalchemy import text

from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_READER = "bfx_cutover_reader"

GRANTS: dict[str, tuple[str, ...]] = {
    "offer_claims": (
        "cid",
        "account_id",
        "venue_offer_id",
        "size_usdt",
        "signal_correlation_id",
        "execution_decision_id",
        "occurred_at_ms",
        "last_updated_ms",
        "last_event_seq",
    ),
    "funding_trades": (
        "exchange_account_id",
        "trade_id",
        "deployment_environment",
        "symbol",
        "mts_create",
        "offer_id",
        "amount",
        "rate",
        "period",
        "maker",
        "recorded_at",
    ),
    "submission_attempt_journal": ("normalized_payload", "seed_provenance"),
    "ledger_observation": ("origin",),
    "trading_state": ("id",),
    "uncertainty_resolution_requests": ("request_id", "outcome_reason"),
    "capital_policy_requests": ("request_id",),
    "trading_control_requests": ("request_id",),
    "venue_offer_state": ("exchange_account_id", "deployment_environment", "symbol", "is_terminal"),
    "venue_credit_state": ("exchange_account_id", "deployment_environment", "symbol", "is_terminal"),
}


def _reader_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": _READER})
        .scalar()
    )


def upgrade() -> None:
    if not _reader_exists():
        return
    for table, columns in GRANTS.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON public.{table} TO {_READER}")


def downgrade() -> None:
    if not _reader_exists():
        return
    for table, columns in GRANTS.items():
        op.execute(f"REVOKE SELECT ({', '.join(columns)}) ON public.{table} FROM {_READER}")
