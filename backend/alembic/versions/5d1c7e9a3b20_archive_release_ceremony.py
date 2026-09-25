"""Archive the retired release ceremony: sessions, canary permits and trading_halt.

ADR 2026-09-25 (automated probation replaces the release ceremony): the
four-step real-money canary, its one-shot permits and the ``trading_halt`` epoch
they were bound to are gone from the application. Their rows are real-money
history -- each consumed permit is a canary offer placed with real funds -- so
they are archived, not deleted:

- The four tables move unchanged into the ``release_archive`` schema
  (``ALTER TABLE ... SET SCHEMA``): every row, column, constraint and index is
  kept, and the foreign keys still tie them to the accounts, decisions and
  submission attempts they name in ``public`` (RESTRICT), so that history
  cannot be orphaned by a later delete.
- Their guard functions and the release operator check move with them.
- Every archived table refuses INSERT/UPDATE/DELETE/TRUNCATE: the archive is
  frozen.
- ``release_archive.manifest`` records, per table, the row count and a SHA-256
  over the rows in primary-key order (``to_jsonb(row)::text`` joined by
  newlines). Anyone can recompute it with the query in ``_DIGEST`` and see the
  archive still holds exactly what was moved.
- No application role has USAGE on the schema, so none can read or write it
  (the tables' old grants stay dormant, which keeps the downgrade lossless).
  The database owner queries it with plain SQL; it is in every backup.

``trading_state.legacy_halt_id`` keeps naming the archived halt a seeded state
came from (it never had a foreign key). Downgrade moves everything back to
``public``; nothing is lost in either direction.
"""
from alembic import op

revision = "5d1c7e9a3b20"
down_revision = "b8e2d4f6a013"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

SCHEMA = "release_archive"
# (table, primary key) in the order the manifest lists them.
TABLES = (
    ("trading_halt", "id"),
    ("canary_command_permits", "permit_id"),
    ("release_sessions", "id"),
    ("release_session_audit", "id"),
)
FUNCTIONS = (
    "guard_release_session()",
    "guard_release_permit()",
    "reject_release_mutation()",
    "release_operator_authorized(uuid,text)",
)
_APP_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth")


def _digest(schema: str, table: str, key: str) -> str:
    return (f"SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text, "
            f"E'\\n' ORDER BY t.{key}), ''), 'UTF8')), 'hex') FROM {schema}.{table} t")


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"""DO $$ DECLARE r text; BEGIN
      FOREACH r IN ARRAY ARRAY{list(_APP_ROLES)} LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
          EXECUTE format('REVOKE ALL ON SCHEMA {SCHEMA} FROM %I', r);
        END IF;
      END LOOP; END $$""")
    for table, _key in TABLES:
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA {SCHEMA}")
    for function in FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{function} SET SCHEMA {SCHEMA}")

    op.execute(f"""CREATE TABLE {SCHEMA}.manifest (
        table_name text PRIMARY KEY,
        row_count bigint NOT NULL CHECK (row_count >= 0),
        content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
        archived_at timestamptz NOT NULL DEFAULT now(),
        archived_by_revision text NOT NULL)""")
    for table, key in TABLES:
        op.execute(f"""INSERT INTO {SCHEMA}.manifest (table_name, row_count, content_sha256,
            archived_by_revision) SELECT '{table}', d.count, d.encode, '{revision}'
            FROM ({_digest(SCHEMA, table, key)}) AS d""")

    op.execute(f"""CREATE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'release_archive is frozen: % on %', TG_OP, TG_TABLE_NAME;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_mutation() FROM PUBLIC")
    # Statement-level, so even a write that would touch no row is refused.
    for table in (*(name for name, _ in TABLES), "manifest"):
        op.execute(f"CREATE TRIGGER archive_frozen BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE "
                   f"ON {SCHEMA}.{table} FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_mutation()")


def downgrade() -> None:
    for table in (*(name for name, _ in TABLES), "manifest"):
        op.execute(f"DROP TRIGGER archive_frozen ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_mutation()")
    op.execute(f"DROP TABLE {SCHEMA}.manifest")
    for function in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {SCHEMA}.{function} SET SCHEMA public")
    for table, _key in reversed(TABLES):
        op.execute(f"ALTER TABLE {SCHEMA}.{table} SET SCHEMA public")
    op.execute(f"DROP SCHEMA {SCHEMA}")
