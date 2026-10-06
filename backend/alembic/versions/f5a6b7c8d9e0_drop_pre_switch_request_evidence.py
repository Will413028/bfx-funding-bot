"""Drop the pre-switch evidence columns of ``uncertainty_resolution_requests`` (contract, part 2).

S1-8 Cleanup-2a. ``e4f5a6b7c8d9`` closed ``reconcile_event_seq`` and ``resolved_event_seq``:
no role may write them, ``observation_id`` is NOT NULL and the evidence CHECK leaves both NULL
on every row. The image deployed before this one (3258f76e) no longer maps them, so nothing
its web API sends while this migration runs names them. This revision:

* refuses a database where any request carries either column (the CHECKs already rule it out;
  this guards a database whose CHECKs were changed by hand);
* rewrites ``guard_uncertainty_resolution_request``: plpgsql resolves ``NEW.<column>`` when the
  trigger fires, so a guard that still named a dropped column would fail every worker UPDATE;
* replaces ``ck_uncertainty_resolution_requests_outcome_shape`` with its form without the two
  columns, and drops ``ck_uncertainty_resolution_requests_evidence`` (``observation_id`` NOT
  NULL is the whole rule now);
* drops the two columns.

Downgrade restores exactly what ``e4f5a6b7c8d9`` had: the columns (nullable, no column grant),
both CHECKs and the guard with its column list.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""

from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None
# One request table's columns, CHECKs and guard: no ledger table, cursor or archive changes.
ledger_contract = "preserved"

TABLE = "public.uncertainty_resolution_requests"
DROPPED_COLUMNS: tuple[str, ...] = ("reconcile_event_seq", "resolved_event_seq")
GUARD = "guard_uncertainty_resolution_request"
SHAPE_CHECK = "ck_uncertainty_resolution_requests_outcome_shape"
EVIDENCE_CHECK = "ck_uncertainty_resolution_requests_evidence"

# The outbox column split after this revision (pinned to the model by
# tests/modules/execution/test_operator_request_schema.py).
UNCERTAINTY_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "observation_id,venue_offer_id,decision,reason,requested_by,created_at_ms"
)
UNCERTAINTY_WORKER_COLUMNS = "state,processed_at_ms,outcome_reason"
# b8c9d0e1f2a4's request columns, which the downgrade's guard pins again.
_PREVIOUS_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "reconcile_event_seq,observation_id,venue_offer_id,decision,reason,requested_by,"
    "created_at_ms"
)

SHAPE = (
    "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
    "(state = 'applied' AND processed_at_ms IS NOT NULL AND outcome_reason IS NULL) OR "
    "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
    "AND outcome_reason IS NOT NULL)"
)
# b8c9d0e1f2a4's shape and evidence CHECKs, verbatim.
_PREVIOUS_SHAPE = (
    "(state = 'requested' AND processed_at_ms IS NULL AND resolved_event_seq IS NULL "
    "AND outcome_reason IS NULL) OR "
    "(state = 'applied' AND processed_at_ms IS NOT NULL "
    "AND (reconcile_event_seq IS NOT NULL) = (resolved_event_seq IS NOT NULL) "
    "AND outcome_reason IS NULL) OR "
    "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
    "AND resolved_event_seq IS NULL AND outcome_reason IS NOT NULL)"
)
_PREVIOUS_EVIDENCE = "(reconcile_event_seq IS NULL) <> (observation_id IS NULL)"


def _guard(columns: str) -> None:
    """b8c9d0e1f2a4's request guard (with the applied-requires-journal rule) over ``columns``."""
    names = columns.split(",")
    op.execute(f"""CREATE OR REPLACE FUNCTION public.{GUARD}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
          THEN RAISE EXCEPTION 'immutable uncertainty resolution request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid uncertainty resolution transition'; END IF;
        IF NEW.state = 'applied' AND NEW.observation_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM public.execution_resolution_journal j
          WHERE j.operator_request_id = NEW.request_id)
          THEN RAISE EXCEPTION 'ledger resolution request applied without a journal row'; END IF;
        RETURN NEW; END $$""")


def upgrade() -> None:
    present = " OR ".join(f"{column} IS NOT NULL" for column in DROPPED_COLUMNS)
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM {TABLE} WHERE {present}) THEN
        RAISE EXCEPTION 'refuse to drop the pre-switch evidence columns: % request(s) of '
          '{TABLE} carry reconcile_event_seq or resolved_event_seq',
          (SELECT count(*) FROM {TABLE} WHERE {present});
      END IF;
    END $$""")
    _guard(UNCERTAINTY_REQUEST_COLUMNS)
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {SHAPE_CHECK}")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {EVIDENCE_CHECK}")
    for column in DROPPED_COLUMNS:
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {SHAPE_CHECK} CHECK ({SHAPE})")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {SHAPE_CHECK}")
    for column in DROPPED_COLUMNS:
        op.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} bigint NULL")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {SHAPE_CHECK} CHECK ({_PREVIOUS_SHAPE})")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {EVIDENCE_CHECK} CHECK ({_PREVIOUS_EVIDENCE})")
    _guard(_PREVIOUS_REQUEST_COLUMNS)
