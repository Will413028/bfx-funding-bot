"""Add explicit capital policy and immutable observation authority; no policy seed.

Revision ID: a9d3e5f7b102
Revises: f8c2d4e6a901
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a9d3e5f7b102"
down_revision = "f8c2d4e6a901"
branch_labels = None
depends_on = None


def _account():
    return sa.Column("exchange_account_id", sa.Uuid(),
                     sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False)


def upgrade() -> None:
    op.create_table("capital_policy_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True), _account(),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("exchange_account_id", "deployment_environment", "symbol", "revision",
                            name="uq_capital_policy_revision_scope"))
    op.create_table("capital_policy_heads", _account(),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "symbol"))
    op.create_table("capital_snapshot_queries",
        sa.Column("id", sa.Uuid(), primary_key=True), _account(),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("command_fence", sa.BigInteger(), nullable=False),
        sa.Column("query_revision", sa.BigInteger(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("exchange_account_id", "deployment_environment", "query_revision",
                            name="uq_capital_query_revision"))
    op.create_table("capital_snapshots",
        sa.Column("event_seq", sa.BigInteger(), sa.ForeignKey("event_log.event_seq", ondelete="RESTRICT"), primary_key=True),
        sa.Column("query_id", sa.Uuid(), sa.ForeignKey("capital_snapshot_queries.id", ondelete="RESTRICT"), nullable=False, unique=True),
        _account(), sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("command_fence", sa.BigInteger(), nullable=False),
        sa.Column("classification", postgresql.JSONB(), nullable=False))
    # Trigger functions are DB-only integrity controls, not ORM entities.
    op.execute("""CREATE FUNCTION public.reject_capital_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable capital evidence'; END $$""")
    for table in ("capital_policy_revisions", "capital_snapshot_queries", "capital_snapshots"):
        op.execute(f"CREATE TRIGGER immutable_capital_write BEFORE UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.reject_capital_mutation()")
        op.execute(f"CREATE TRIGGER immutable_capital_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION public.reject_capital_mutation()")
    # Authorization is carried by immutable intent, not a mutable reservation row.
    # Protect the event even before the capital_snapshots FK exists in this txn.
    op.execute("""CREATE FUNCTION public.guard_capital_event() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF OLD.payload->'capital_authorization' IS NOT NULL
             AND OLD.payload->'capital_authorization' <> 'null'::jsonb
             OR OLD.payload->>'capital_query_id' IS NOT NULL THEN
            RAISE EXCEPTION 'immutable capital event';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER immutable_capital_event BEFORE UPDATE OR DELETE ON public.event_log FOR EACH ROW EXECUTE FUNCTION public.guard_capital_event()")
    # Do not grant a new principal any rights. Remove inherited dangerous CRUD
    # defaults ONLY from the known restricted runtime, if it exists. Operator
    # application of policy uses an appropriately privileged separate connection.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bfx_bot') THEN
          REVOKE UPDATE, DELETE, TRUNCATE ON public.capital_snapshot_queries, public.capital_snapshots FROM bfx_bot;
          REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.capital_policy_revisions, public.capital_policy_heads FROM bfx_bot;
        END IF;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.reject_capital_mutation(), public.guard_capital_event() FROM PUBLIC")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.capital_policy_revisions)
           OR EXISTS (SELECT FROM public.capital_snapshot_queries)
           OR EXISTS (SELECT FROM public.capital_snapshots) THEN
          RAISE EXCEPTION 'refuse downgrade of populated capital evidence';
        END IF;
        END $$""")
    op.execute("DROP TRIGGER immutable_capital_event ON public.event_log")
    for table in ("capital_snapshots", "capital_snapshot_queries", "capital_policy_heads", "capital_policy_revisions"):
        op.drop_table(table)
    op.execute("DROP FUNCTION public.reject_capital_mutation(), public.guard_capital_event()")
