"""The web API's baseline privileges, versioned instead of a runbook step.

Runbook ``halt-1-exchange-account-cutover`` 6b granted these by hand. The web API
depends on every one of them -- the account scope, the projections and
uncertainty context, the trading-control overview's probation progress
(``submission_attempts``), attribution, candles, the credential and config draft
flows, and ``/ready`` -- so a database migrated without the runbook step served
403s or 500s. From this revision the migration grants exactly that baseline and
nothing more: reads of the read models, and the web API's own account-setup
writes. Every execution write stays the daemon's (ADR D4'); later outbox
migrations keep adding only column-scoped request INSERTs.

Downgrade keeps the grants: they predate this revision (the runbook gave them),
and taking them away is a decision for whoever downgrades, not a side effect.
"""
from alembic import op

revision = "6f2b8d0e4a17"
down_revision = "5d1c7e9a3b20"
branch_labels = None
depends_on = None

READ = (
    "user_profiles", "exchange_accounts", "exchange_account_memberships",
    "position_state", "offer_claims", "event_log", "execution_uncertainties",
    "submission_attempts", "attribution_weekly", "funding_candles",
)
WRITE = {
    "user_profiles": "SELECT, INSERT",
    "exchange_account_credentials": "SELECT, INSERT, UPDATE",
    "account_config_drafts": "SELECT, INSERT, UPDATE, DELETE",
}


def upgrade() -> None:
    reads = ", ".join(f"public.{table}" for table in READ)
    writes = "\n".join(f"        GRANT {privileges} ON public.{table} TO bfx_webapi;"
                       for table, privileges in WRITE.items())
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        GRANT USAGE ON SCHEMA public TO bfx_webapi;
        -- /ready compares the database revision with the image's graph.
        GRANT SELECT (version_num) ON public.alembic_version TO bfx_webapi;
        GRANT SELECT ON {reads} TO bfx_webapi;
{writes}
      END IF; END $$""")


def downgrade() -> None:
    pass
