"""Close the uncertainty requests' pre-switch evidence columns to their writers.

S1-8 cleanup. ``uncertainty_resolution_requests.reconcile_event_seq`` (the legacy reconcile
event a request cited) and ``resolved_event_seq`` (the legacy event its resolution appended)
belong to requests made before the switch. Under the ledger, a request cites an observation
and its resolution appends no event (``ck_..._evidence``, ``ck_..._outcome_shape``), and every
process refuses a database not on the ledger, so nothing may write either column again:

* revoke ``bfx_webapi``'s INSERT (``reconcile_event_seq``) and ``bfx_bot``'s UPDATE
  (``resolved_event_seq``), the column grants ``1c435a35dcb4`` gave them;
* recreate ``guard_uncertainty_request_evidence_epoch`` (``b8c9d0e1f2a4``) without its branch
  refusing reconcile evidence under the ledger epoch: no non-owner can write the column now.
  The branch refusing observation evidence before the switch stays.

Rows keep their values (append-only history; production had no row with either set). Downgrade
grants both back to the roles that exist and recreates the function as ``b8c9d0e1f2a4`` left it.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""

from sqlalchemy import text

from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None
# Grants and a request-table trigger function only: no ledger table, cursor or archive change.
ledger_contract = "preserved"

_TABLE = "public.uncertainty_resolution_requests"
_FUNCTION = "guard_uncertainty_request_evidence_epoch"
# (role, privilege, column) given by 1c435a35dcb4 and taken back here.
GRANTS: tuple[tuple[str, str, str], ...] = (
    ("bfx_webapi", "INSERT", "reconcile_event_seq"),
    ("bfx_bot", "UPDATE", "resolved_event_seq"),
)
_RECONCILE_BRANCH = """
            IF NEW.reconcile_event_seq IS NOT NULL AND ledger_epoch IS TRUE THEN
              RAISE EXCEPTION 'reconcile evidence is closed under ledger authority';
            END IF;"""


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _function(*, reconcile_branch: bool) -> None:
    """``b8c9d0e1f2a4``'s function; ``reconcile_branch`` is the branch this revision drops.

    Not SECURITY DEFINER (same owner exemption as guard_ledger_authority): the epoch read
    needs the writing role's own SELECT, which bfx_webapi holds."""
    branch = _RECONCILE_BRANCH if reconcile_branch else ""
    op.execute(f"""CREATE OR REPLACE FUNCTION public.{_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE ledger_epoch boolean;
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID) THEN
            ledger_epoch := (SELECT e.authority FROM public.capital_authority_epoch e
                               ORDER BY e.epoch_seq DESC LIMIT 1) IS NOT DISTINCT FROM 'ledger';
            IF NEW.observation_id IS NOT NULL AND ledger_epoch IS NOT TRUE THEN
              RAISE EXCEPTION 'observation evidence requires ledger authority';
            END IF;{branch}
          END IF;
          RETURN NEW;
        END $$""")


def upgrade() -> None:
    for role, privilege, column in GRANTS:
        if _role_exists(role):
            op.execute(f"REVOKE {privilege} ({column}) ON {_TABLE} FROM {role}")
    _function(reconcile_branch=False)


def downgrade() -> None:
    _function(reconcile_branch=True)
    for role, privilege, column in GRANTS:
        if _role_exists(role):
            op.execute(f"GRANT {privilege} ({column}) ON {_TABLE} TO {role}")
