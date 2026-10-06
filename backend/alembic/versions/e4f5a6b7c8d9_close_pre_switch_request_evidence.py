"""Close the pre-switch evidence columns of ``uncertainty_resolution_requests`` (contract, part 1).

S1-8 Cleanup-2a. Since cb6b4a80 no code writes ``reconcile_event_seq`` (the legacy reconcile
event a pre-switch request cited) or ``resolved_event_seq`` (the legacy event its resolution
appended). This revision takes away what still lets anyone write them:

* refuses a database where any request carries either column (production has none: every
  request since the switch cites a ledger observation);
* drops ``uncertainty_request_evidence_epoch`` and its function
  ``guard_uncertainty_request_evidence_epoch`` (``b8c9d0e1f2a4``): its observation branch is
  dead (every database at head has a ``ledger`` epoch since the genesis of ``b1c2d3e4f5a6``)
  and its reconcile branch is replaced by the NOT NULL below, which (with the evidence CHECK)
  holds for the owner too; the revoke covers only non-owner roles;
* revokes every column privilege on the two columns (the web API's INSERT and the account
  writer's UPDATE from ``1c435a35dcb4``, and any an environment added);
* makes ``observation_id`` NOT NULL. With ``ck_uncertainty_resolution_requests_evidence``
  (exactly one evidence column) that leaves ``reconcile_event_seq`` NULL on every row.

The columns themselves stay one more release: the image deployed before this one maps them,
so its web API's ORM reads of the table (``select(UncertaintyResolutionRequestRow)``) name
them, and that web API keeps serving while this migration runs. The release after this one,
whose predecessor no longer maps them, drops them with the CHECKs and the request guard's
column list that still name them.

Downgrade restores exactly what ``d3e4f5a6b7c8`` had: ``observation_id`` nullable, the
migrations' column grants (to roles that exist), and the trigger with its function.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""

from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None
# One request table's columns and grants: no ledger table, cursor or archive changes.
ledger_contract = "preserved"

TABLE = "public.uncertainty_resolution_requests"
CLOSED_COLUMNS: tuple[str, ...] = ("reconcile_event_seq", "resolved_event_seq")
EPOCH_TRIGGER = "uncertainty_request_evidence_epoch"
EPOCH_FUNCTION = "guard_uncertainty_request_evidence_epoch"
# What the migrations granted on the closed columns (1c435a35dcb4), and what the downgrade
# grants back.
MIGRATION_GRANTS: tuple[tuple[str, str, str], ...] = (
    ("bfx_webapi", "INSERT", "reconcile_event_seq"),
    ("bfx_bot", "UPDATE", "resolved_event_seq"),
)

# b8c9d0e1f2a4's function, verbatim.
_EPOCH_FUNCTION_SQL = f"""CREATE FUNCTION public.{EPOCH_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE ledger_epoch boolean;
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID) THEN
            ledger_epoch := (SELECT e.authority FROM public.capital_authority_epoch e
                               ORDER BY e.epoch_seq DESC LIMIT 1) IS NOT DISTINCT FROM 'ledger';
            IF NEW.observation_id IS NOT NULL AND ledger_epoch IS NOT TRUE THEN
              RAISE EXCEPTION 'observation evidence requires ledger authority';
            END IF;
            IF NEW.reconcile_event_seq IS NOT NULL AND ledger_epoch IS TRUE THEN
              RAISE EXCEPTION 'reconcile evidence is closed under ledger authority';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""


def upgrade() -> None:
    present = " OR ".join(f"{column} IS NOT NULL" for column in CLOSED_COLUMNS)
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM {TABLE} WHERE {present}) THEN
        RAISE EXCEPTION 'refuse to close the pre-switch evidence columns: % request(s) of '
          '{TABLE} carry reconcile_event_seq or resolved_event_seq',
          (SELECT count(*) FROM {TABLE} WHERE {present});
      END IF;
    END $$""")
    op.execute(f"DROP TRIGGER {EPOCH_TRIGGER} ON {TABLE}")
    op.execute(f"DROP FUNCTION public.{EPOCH_FUNCTION}()")
    closed = ", ".join(f"'{column}'" for column in CLOSED_COLUMNS)
    # Every grantee's column privileges, PUBLIC included (grantee 0); the owner holds none.
    op.execute(f"""DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT DISTINCT att.attname,
                 CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END
                   AS grantee
               FROM pg_attribute att, aclexplode(att.attacl) a
               WHERE att.attrelid = '{TABLE}'::regclass AND att.attname IN ({closed})
      LOOP
        EXECUTE format('REVOKE ALL (%I) ON {TABLE} FROM %s', r.attname, r.grantee);
      END LOOP;
      IF EXISTS (SELECT 1 FROM pg_attribute att, aclexplode(att.attacl) a
                 WHERE att.attrelid = '{TABLE}'::regclass AND att.attname IN ({closed})) THEN
        RAISE EXCEPTION 'a column privilege on the pre-switch evidence columns survived';
      END IF;
    END $$""")
    op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN observation_id SET NOT NULL")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN observation_id DROP NOT NULL")
    for role, privilege, column in MIGRATION_GRANTS:
        op.execute(f"""DO $$ BEGIN
          IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
            GRANT {privilege} ({column}) ON {TABLE} TO {role};
          END IF;
        END $$""")
    op.execute(_EPOCH_FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION public.{EPOCH_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {EPOCH_TRIGGER} BEFORE INSERT ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{EPOCH_FUNCTION}()"
    )
