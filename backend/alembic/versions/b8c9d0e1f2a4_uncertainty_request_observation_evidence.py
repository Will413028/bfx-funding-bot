"""Let an uncertainty resolution request cite a ledger observation instead of a reconcile event.

``uncertainty_resolution_requests`` gains a nullable ``observation_id`` (FK to
``ledger_observation``); ``reconcile_event_seq`` becomes nullable and exactly one of
the two is set. A ledger-evidence row is ``applied`` with no ``resolved_event_seq``
(no event is appended) and only once an ``execution_resolution_journal`` row cites
its ``request_id``; the journal side is made one-to-one by a partial UNIQUE index.
The web API may write ``observation_id`` only under the ``ledger`` authority epoch
and ``reconcile_event_seq`` only before it. Schema and legacy alignment only: no
read or write path uses ``observation_id`` yet.

Revision ID: b8c9d0e1f2a4
Revises: a7b8c9d0e1f3
"""

from sqlalchemy import text

from alembic import op

revision = "b8c9d0e1f2a4"
down_revision = "a7b8c9d0e1f3"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_TABLE = "uncertainty_resolution_requests"
_EVIDENCE_CHECK = "ck_uncertainty_resolution_requests_evidence"
_SHAPE_CHECK = "ck_uncertainty_resolution_requests_outcome_shape"
_FK = "fk_uncertainty_resolution_requests_observation"
_JOURNAL_INDEX = "uq_execution_resolution_operator_request"
_GUARD = "guard_uncertainty_resolution_request"
_EPOCH_FUNCTION = "guard_uncertainty_request_evidence_epoch"
_EPOCH_TRIGGER = "uncertainty_request_evidence_epoch"
_EPOCH = "capital_authority_epoch"

UNCERTAINTY_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "reconcile_event_seq,observation_id,venue_offer_id,decision,reason,requested_by,"
    "created_at_ms"
)
UNCERTAINTY_WORKER_COLUMNS = "state,processed_at_ms,resolved_event_seq,outcome_reason"
_OLD_REQUEST_COLUMNS = (
    "request_id,exchange_account_id,deployment_environment,uncertainty_id,action,"
    "reconcile_event_seq,venue_offer_id,decision,reason,requested_by,created_at_ms"
)

_OLD_SHAPE = (
    "(state = 'requested' AND processed_at_ms IS NULL AND resolved_event_seq IS NULL "
    "AND outcome_reason IS NULL) OR "
    "(state = 'applied' AND processed_at_ms IS NOT NULL AND resolved_event_seq IS NOT NULL "
    "AND outcome_reason IS NULL) OR "
    "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
    "AND resolved_event_seq IS NULL AND outcome_reason IS NOT NULL)"
)
_NEW_SHAPE = (
    "(state = 'requested' AND processed_at_ms IS NULL AND resolved_event_seq IS NULL "
    "AND outcome_reason IS NULL) OR "
    "(state = 'applied' AND processed_at_ms IS NOT NULL "
    "AND (reconcile_event_seq IS NOT NULL) = (resolved_event_seq IS NOT NULL) "
    "AND outcome_reason IS NULL) OR "
    "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
    "AND resolved_event_seq IS NULL AND outcome_reason IS NOT NULL)"
)


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _guard_function(columns: str, *, journal_required: bool) -> None:
    """The request guard of 1c435a35dcb4; ``journal_required`` adds the ledger-evidence rule."""
    names = columns.split(",")
    journal_rule = (
        """IF NEW.state = 'applied' AND NEW.observation_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM public.execution_resolution_journal j
          WHERE j.operator_request_id = NEW.request_id)
          THEN RAISE EXCEPTION 'ledger resolution request applied without a journal row'; END IF;
        """
        if journal_required
        else ""
    )
    op.execute(f"""CREATE OR REPLACE FUNCTION public.{_GUARD}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
          THEN RAISE EXCEPTION 'immutable uncertainty resolution request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid uncertainty resolution transition'; END IF;
        {journal_rule}RETURN NEW; END $$""")


def upgrade() -> None:
    op.execute(f"ALTER TABLE public.{_TABLE} ADD COLUMN observation_id uuid NULL")
    op.execute(
        f"ALTER TABLE public.{_TABLE} ADD CONSTRAINT {_FK} FOREIGN KEY (observation_id) "
        "REFERENCES public.ledger_observation (id) ON DELETE RESTRICT"
    )
    op.execute(f"ALTER TABLE public.{_TABLE} ALTER COLUMN reconcile_event_seq DROP NOT NULL")
    op.execute(
        f"ALTER TABLE public.{_TABLE} ADD CONSTRAINT {_EVIDENCE_CHECK} "
        "CHECK ((reconcile_event_seq IS NULL) <> (observation_id IS NULL))"
    )
    op.execute(f"ALTER TABLE public.{_TABLE} DROP CONSTRAINT {_SHAPE_CHECK}")
    op.execute(f"ALTER TABLE public.{_TABLE} ADD CONSTRAINT {_SHAPE_CHECK} CHECK ({_NEW_SHAPE})")
    op.execute(
        f"CREATE UNIQUE INDEX {_JOURNAL_INDEX} ON public.execution_resolution_journal "
        "(operator_request_id) WHERE operator_request_id IS NOT NULL"
    )
    _guard_function(UNCERTAINTY_REQUEST_COLUMNS, journal_required=True)

    # Not SECURITY DEFINER (same owner exemption as guard_ledger_authority): the
    # epoch read needs the writing role's own SELECT, which bfx_webapi holds.
    op.execute(f"""CREATE FUNCTION public.{_EPOCH_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE ledger_epoch boolean;
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID) THEN
            ledger_epoch := (SELECT e.authority FROM public.{_EPOCH} e
                               ORDER BY e.epoch_seq DESC LIMIT 1) IS NOT DISTINCT FROM 'ledger';
            IF NEW.observation_id IS NOT NULL AND ledger_epoch IS NOT TRUE THEN
              RAISE EXCEPTION 'observation evidence requires ledger authority';
            END IF;
            IF NEW.reconcile_event_seq IS NOT NULL AND ledger_epoch IS TRUE THEN
              RAISE EXCEPTION 'reconcile evidence is closed under ledger authority';
            END IF;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_EPOCH_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {_EPOCH_TRIGGER} BEFORE INSERT ON public.{_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_EPOCH_FUNCTION}()"
    )
    # Bot grants are unchanged (SELECT table-wide, UPDATE on the worker columns).
    if _role_exists("bfx_webapi"):
        op.execute(f"GRANT INSERT (observation_id) ON public.{_TABLE} TO bfx_webapi")


def downgrade() -> None:
    # Ledger-evidence rows cannot be expressed in the older contract.
    if (
        op.get_bind()
        .execute(
            text(
                f"SELECT EXISTS (SELECT 1 FROM public.{_TABLE} "
                "WHERE observation_id IS NOT NULL OR reconcile_event_seq IS NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError(f"refuse downgrade with ledger-evidence rows: {_TABLE}")
    op.execute(f"DROP TRIGGER {_EPOCH_TRIGGER} ON public.{_TABLE}")
    op.execute(f"DROP FUNCTION public.{_EPOCH_FUNCTION}()")
    _guard_function(_OLD_REQUEST_COLUMNS, journal_required=False)
    op.execute(f"DROP INDEX public.{_JOURNAL_INDEX}")
    op.execute(f"ALTER TABLE public.{_TABLE} DROP CONSTRAINT {_SHAPE_CHECK}")
    op.execute(f"ALTER TABLE public.{_TABLE} DROP CONSTRAINT {_EVIDENCE_CHECK}")
    # Dropping the column also drops its column grant and foreign key.
    op.execute(f"ALTER TABLE public.{_TABLE} DROP COLUMN observation_id")
    op.execute(f"ALTER TABLE public.{_TABLE} ALTER COLUMN reconcile_event_seq SET NOT NULL")
    op.execute(f"ALTER TABLE public.{_TABLE} ADD CONSTRAINT {_SHAPE_CHECK} CHECK ({_OLD_SHAPE})")
