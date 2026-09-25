"""Deployment ledger, the web API's baseline grants, and the release ceremony archive.

Revises 1c435a35dcb4 (trading governance). ADR 2026-09-25 and
2026-09-25-ci-registry-digest-deploy D3.

``deployments`` -- append-only ledger of VM deployment attempts (bfx-deploy):
source revision, backend/frontend digests, change class, whether migrations
ran, outcome. An attempt that changes the containers has two rows sharing
``attempt_id``: ``started`` (appended just before the containers are
(re)created; ``attempt_id`` is the ``BFX_DEPLOYMENT_ID`` the containers get, so
the booting bot reads its own row) and a terminal ``deployed``,
``rolled_back`` or ``failed``. An attempt that stops earlier has only the
terminal row. ``check_deployment_attempt`` keeps the pair consistent: one row
of each phase, the started row first, the terminal row naming the same release,
class and start. The host tool writes it as the owner through ``docker exec
bfx-postgres psql``; the runtime roles only read it.

Web API baseline -- what runbook ``halt-1-exchange-account-cutover`` 6b used to
grant by hand: reads of the read models (account scope, projections and
uncertainty context, ``submission_attempts`` for probation progress,
attribution, candles, ``alembic_version`` for ``/ready``) and the web API's own
account-setup writes. Every execution write stays the daemon's (ADR D4').

``release_archive`` -- the retired release ceremony (``trading_halt``,
``canary_command_permits``, ``release_sessions``, ``release_session_audit``) is
real-money history, so it is archived, not deleted:

- the four tables move unchanged (``ALTER TABLE ... SET SCHEMA``): every row,
  column, constraint and index is kept, and their foreign keys still tie them
  to the accounts, decisions and submission attempts in ``public`` (RESTRICT);
- their guard functions and the release operator check move with them;
- every archived table refuses INSERT/UPDATE/DELETE/TRUNCATE (statement-level);
- ``release_archive.manifest`` records per table the row count and a SHA-256
  over the rows in primary-key order (``to_jsonb(row)::text`` joined by
  newlines; recompute with ``_digest``);
- no application role has USAGE on the schema. The tables' old grants stay
  dormant, which keeps the downgrade lossless.

``trading_state.legacy_halt_id`` keeps naming the archived halt a carried-over
state came from (it never had a foreign key).

Downgrade moves the archive back to ``public`` unchanged and drops the ledger.
The baseline grants stay: they predate this revision (the runbook gave them),
and taking them away is a decision for whoever downgrades.
"""
import sqlalchemy as sa

from alembic import op

revision = "c74d45a54e46"
down_revision = "1c435a35dcb4"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"

WEBAPI_READ = (
    "user_profiles", "exchange_accounts", "exchange_account_memberships",
    "position_state", "offer_claims", "event_log", "execution_uncertainties",
    "submission_attempts", "attribution_weekly", "funding_candles",
)
WEBAPI_WRITE = {
    "user_profiles": "SELECT, INSERT",
    "exchange_account_credentials": "SELECT, INSERT, UPDATE",
    "account_config_drafts": "SELECT, INSERT, UPDATE, DELETE",
}

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


def _create_deployments() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(f"backend_digest ~ {_DIGEST_RE} AND frontend_digest ~ {_DIGEST_RE}",
                           name="ck_deployments_digests"),
        sa.CheckConstraint("change_class IN ('standard', 'material')",
                           name="ck_deployments_change_class"),
        # no_change is never recorded: an up-to-date run writes nothing.
        sa.CheckConstraint("outcome IN ('started', 'deployed', 'rolled_back', 'failed')",
                           name="ck_deployments_outcome"),
        sa.CheckConstraint("(outcome = 'started') = (finished_at IS NULL)",
                           name="ck_deployments_phase_finished"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at",
                           name="ck_deployments_times"),
        sa.CheckConstraint("length(detail) <= 2000", name="ck_deployments_detail"),
        sa.CheckConstraint("ci_run IS NULL OR length(ci_run) BETWEEN 1 AND 200",
                           name="ck_deployments_ci_run"),
    )
    op.create_index("uq_deployments_attempt_started", "deployments", ["attempt_id"], unique=True,
                    postgresql_where=sa.text("outcome = 'started'"))
    op.create_index("uq_deployments_attempt_finished", "deployments", ["attempt_id"], unique=True,
                    postgresql_where=sa.text("outcome <> 'started'"))
    op.execute("""CREATE FUNCTION public.check_deployment_attempt() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE opened public.deployments%ROWTYPE; BEGIN
          SELECT * INTO opened FROM public.deployments
            WHERE attempt_id = NEW.attempt_id AND outcome = 'started';
          IF NEW.outcome = 'started' THEN
            IF EXISTS (SELECT FROM public.deployments WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'deployment attempt % already recorded', NEW.attempt_id;
            END IF;
          ELSIF FOUND AND (opened.source_revision, opened.backend_digest, opened.frontend_digest,
                           opened.change_class, opened.started_at)
                IS DISTINCT FROM (NEW.source_revision, NEW.backend_digest, NEW.frontend_digest,
                                  NEW.change_class, NEW.started_at) THEN
            RAISE EXCEPTION 'deployment attempt % finished as a different release', NEW.attempt_id;
          END IF;
          RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER deployments_attempt_pairing BEFORE INSERT ON public.deployments "
               "FOR EACH ROW EXECUTE FUNCTION public.check_deployment_attempt()")
    op.execute("""CREATE FUNCTION public.reject_deployment_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          RAISE EXCEPTION 'deployments ledger is append-only'; END $$""")
    op.execute("CREATE TRIGGER deployments_append_only BEFORE UPDATE OR DELETE ON public.deployments "
               "FOR EACH ROW EXECUTE FUNCTION public.reject_deployment_mutation()")
    op.execute("CREATE TRIGGER deployments_no_truncate BEFORE TRUNCATE ON public.deployments "
               "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_deployment_mutation()")
    op.execute("REVOKE ALL ON FUNCTION public.check_deployment_attempt(), "
               "public.reject_deployment_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.deployments FROM PUBLIC")
    # Read-only for the runtime roles; only the owner, via bfx-deploy, inserts.
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


def _grant_webapi_baseline() -> None:
    reads = ", ".join(f"public.{table}" for table in WEBAPI_READ)
    writes = "\n".join(f"        GRANT {privileges} ON public.{table} TO bfx_webapi;"
                       for table, privileges in WEBAPI_WRITE.items())
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        GRANT USAGE ON SCHEMA public TO bfx_webapi;
        -- /ready compares the database revision with the image's graph.
        GRANT SELECT (version_num) ON public.alembic_version TO bfx_webapi;
        GRANT SELECT ON {reads} TO bfx_webapi;
{writes}
      END IF; END $$""")


def _archive_release_ceremony() -> None:
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


def upgrade() -> None:
    _create_deployments()
    _grant_webapi_baseline()
    _archive_release_ceremony()


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

    op.drop_table("deployments")
    op.execute("DROP FUNCTION public.check_deployment_attempt(), public.reject_deployment_mutation()")
