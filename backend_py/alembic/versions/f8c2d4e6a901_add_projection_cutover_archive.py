"""Add operator-owned immutable projection archive; no live data writes.

Revision ID: f8c2d4e6a901
Revises: e7b1c2d3e4f5

Run as a dedicated migration/operator principal. This migration does NOT demote
runtime roles. A superuser runtime remains a deployment blocker; REVOKE cannot
constrain it. Operator provisioning/grants inventory is a separate review.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f8c2d4e6a901"
down_revision = "e7b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA projection_audit")
    op.create_table(
        "runs",
        sa.Column("run_id", postgresql.UUID(), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("manifest", sa.LargeBinary(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        schema="projection_audit",
    )
    op.create_table(
        "rows",
        sa.Column(
            "run_id",
            postgresql.UUID(),
            sa.ForeignKey("projection_audit.runs.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("table_name", sa.Text(), primary_key=True),
        sa.Column("row_key", sa.LargeBinary(), primary_key=True),
        sa.Column("encoded_payload", sa.LargeBinary(), nullable=False),
        sa.Column("row_digest", sa.Text(), nullable=False),
        schema="projection_audit",
    )
    op.create_table(
        "receipts",
        sa.Column(
            "run_id",
            postgresql.UUID(),
            sa.ForeignKey("projection_audit.runs.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("snapshot_identity", sa.LargeBinary(), nullable=False),
        sa.Column("new_stream", sa.LargeBinary(), nullable=False),
        sa.Column("active_hashes", sa.LargeBinary(), nullable=False),
        sa.Column("evidence", sa.LargeBinary(), nullable=False),
        schema="projection_audit",
    )
    op.execute("""
        CREATE FUNCTION projection_audit.guard_run() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.complete THEN RAISE EXCEPTION 'archive must start incomplete'; END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' OR OLD.complete THEN
            RAISE EXCEPTION 'immutable archive run';
          END IF;
          IF NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.exchange_account_id IS DISTINCT FROM OLD.exchange_account_id
             OR NEW.deployment_environment IS DISTINCT FROM OLD.deployment_environment
             OR NOT NEW.complete THEN
            RAISE EXCEPTION 'archive only permits completion';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION projection_audit.guard_child() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE done boolean;
        BEGIN
          IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable archive child'; END IF;
          SELECT complete INTO done FROM projection_audit.runs
            WHERE run_id = NEW.run_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'archive run missing'; END IF;
          IF (TG_TABLE_NAME = 'rows' AND done)
             OR (TG_TABLE_NAME = 'receipts' AND NOT done) THEN
            RAISE EXCEPTION 'archive run completion mismatch';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION projection_audit.guard_truncate() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable archive cannot truncate'; END $$
    """)
    op.execute("""
        CREATE FUNCTION projection_audit.require_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM projection_audit.runs WHERE run_id = NEW.run_id AND NOT complete) THEN
            RAISE EXCEPTION 'archive must complete in the capture transaction';
          END IF;
          RETURN NULL;
        END $$
    """)
    for name in ("runs", "rows", "receipts"):
        function = "guard_run" if name == "runs" else "guard_child"
        op.execute(
            f"CREATE TRIGGER immutable_write BEFORE INSERT OR UPDATE OR DELETE ON projection_audit.{name} FOR EACH ROW EXECUTE FUNCTION projection_audit.{function}()"
        )
        op.execute(
            f"CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON projection_audit.{name} FOR EACH STATEMENT EXECUTE FUNCTION projection_audit.guard_truncate()"
        )
    op.execute("""
        CREATE CONSTRAINT TRIGGER complete_at_commit AFTER INSERT ON projection_audit.runs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION projection_audit.require_complete()
    """)
    op.execute("REVOKE ALL ON SCHEMA projection_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA projection_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA projection_audit FROM PUBLIC")


def downgrade() -> None:
    # A DO block also makes the guard effective in generated offline SQL.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM projection_audit.runs)
             OR EXISTS (SELECT 1 FROM projection_audit.rows)
             OR EXISTS (SELECT 1 FROM projection_audit.receipts) THEN
            RAISE EXCEPTION 'refuse downgrade of populated projection archive';
          END IF;
        END $$
    """)
    for name in ("receipts", "rows", "runs"):
        op.drop_table(name, schema="projection_audit")
    for name in ("guard_run", "guard_child", "guard_truncate", "require_complete"):
        op.execute(f"DROP FUNCTION projection_audit.{name}()")
    op.execute("DROP SCHEMA projection_audit")
