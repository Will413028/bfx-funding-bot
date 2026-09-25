"""Append-only ledger of VM deployments (bfx-deploy), keyed by image digest.

Revision ID: c3a639388457
Revises: a7f3c1d9e204

ADR 2026-09-25-ci-registry-digest-deploy D3: every deployment attempt that
identified a target is recorded once -- source revision, backend/frontend
digests, change class, whether migrations ran, outcome -- and never edited.
The host tool writes it as the owner role through `docker exec bfx-postgres
psql`; runtime roles only read it. No new database account.

MERGE NOTE: this revision was cut from a7f3c1d9e204 in parallel with other
release-governance migrations; re-chain down_revision onto their head when the
branches meet.
"""
import sqlalchemy as sa

from alembic import op

revision = "c3a639388457"
down_revision = "a7f3c1d9e204"
branch_labels = None
depends_on = None

_DIGEST = "'^sha256:[0-9a-f]{64}$'"


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=False),
        sa.Column("backend_digest", sa.Text(), nullable=False),
        sa.Column("frontend_digest", sa.Text(), nullable=False),
        sa.Column("change_class", sa.Text(), nullable=False),
        sa.Column("migrations_applied", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("ci_run", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("source_revision ~ '^[0-9a-f]{40}$'",
                           name="ck_deployments_source_revision"),
        sa.CheckConstraint(f"backend_digest ~ {_DIGEST} AND frontend_digest ~ {_DIGEST}",
                           name="ck_deployments_digests"),
        sa.CheckConstraint("change_class IN ('standard', 'material')",
                           name="ck_deployments_change_class"),
        # no_change is never recorded: an up-to-date run writes nothing.
        sa.CheckConstraint("outcome IN ('deployed', 'rolled_back', 'failed')",
                           name="ck_deployments_outcome"),
        sa.CheckConstraint("finished_at >= started_at", name="ck_deployments_times"),
        sa.CheckConstraint("length(detail) <= 2000", name="ck_deployments_detail"),
        sa.CheckConstraint("ci_run IS NULL OR length(ci_run) BETWEEN 1 AND 200",
                           name="ck_deployments_ci_run"),
    )
    op.execute("""CREATE FUNCTION public.reject_deployment_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'deployments ledger is append-only'; END $$""")
    op.execute("CREATE TRIGGER deployments_append_only BEFORE UPDATE OR DELETE ON public.deployments "
               "FOR EACH ROW EXECUTE FUNCTION public.reject_deployment_mutation()")
    op.execute("CREATE TRIGGER deployments_no_truncate BEFORE TRUNCATE ON public.deployments "
               "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_deployment_mutation()")
    op.execute("REVOKE ALL ON FUNCTION public.reject_deployment_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.deployments FROM PUBLIC")
    # Read-only for the runtime roles (which release is running, for the UI and
    # for T5's approval); only the owner, via bfx-deploy, ever inserts. REVOKE
    # first: default privileges on the schema may already have granted more.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_webapi') THEN
        REVOKE ALL ON public.deployments FROM bfx_webapi;
        GRANT SELECT ON public.deployments TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_bot') THEN
        REVOKE ALL ON public.deployments FROM bfx_bot;
        GRANT SELECT ON public.deployments TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_webauth') THEN
        REVOKE ALL ON public.deployments FROM bfx_webauth;
      END IF; END $$""")


def downgrade() -> None:
    op.execute("DROP TRIGGER deployments_no_truncate ON public.deployments")
    op.execute("DROP TRIGGER deployments_append_only ON public.deployments")
    op.execute("DROP FUNCTION public.reject_deployment_mutation()")
    op.drop_table("deployments")
