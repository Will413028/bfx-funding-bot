"""Stamp each database with its realm and reject writes of any other realm.

ADR 2026-10-03 D1/E2: one database per realm. ``database_realm`` is a one-row,
insert-only table naming the realm (``prod``, ``shadow`` or ``ci``) this database
belongs to. One generic trigger function is attached ``BEFORE INSERT OR UPDATE OF
deployment_environment`` to every table with that column (the explicit list below; the
integration scan test fails when a new realm table is missing from it). A write whose
realm differs from the stamp is rejected, and so is every realm write while the
database is unstamped (fail-closed). The owner is not exempt: a prod row written into a
shadow database through a wrong DSN fails like any other. A deliberate exception has to
go through a visible ``ALTER TABLE ... DISABLE TRIGGER``. Superuser
``session_replication_role = replica`` still bypasses, which is out of scope: the guard
catches misconfiguration, not a superuser.

The stamp is derived from data at migration time: exactly one realm across all realm
tables is stamped (production holds only ``prod``), more than one realm refuses the
migration (atomic: the schema stays as it was), and no rows leave the database
unstamped; the owner stamps a fresh host or a simulation database once (fresh-host
runbook). The function is SECURITY DEFINER with a pinned ``search_path`` so a writer
role needs no privilege on the stamp, and the web API's exact allowlist is unchanged.

Grants: everything on the stamp is revoked from PUBLIC and the runtime roles, then
``bfx_bot`` gets SELECT (boot check and the simulated venue store). Only the owner
writes the stamp, once.

The downgrade drops the triggers, the function and the table; row data is untouched.

Revision ID: a7c3e9f1b2d4
Revises: e6b1d4a7c9f3
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "a7c3e9f1b2d4"
down_revision = "e6b1d4a7c9f3"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_TABLE = "database_realm"
_FUNCTION = "guard_database_realm"
_MUTATION_FUNCTION = "reject_database_realm_mutation"
_TRIGGER = "database_realm_write"
_REALMS = ("prod", "shadow", "ci")
_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_COLUMNS = ("id", "realm", "stamped_at_ms", "actor")
# Every table with a ``deployment_environment`` column at this revision (schema-qualified).
REALM_TABLES = (
    "projection_audit.runs",
    "public.accepted_capital_basis",
    "public.attribution_weekly",
    "public.capital_command_clock",
    "public.capital_policy_heads",
    "public.capital_policy_requests",
    "public.capital_policy_revisions",
    "public.capital_snapshot_queries",
    "public.capital_snapshots",
    "public.config_regime",
    "public.diagnostics",
    "public.event_log",
    "public.event_prefix_hashes",
    "public.execution_decisions",
    "public.execution_resolution_journal",
    "public.execution_uncertainties",
    "public.funding_cancel_all_audit",
    "public.funding_credit_history",
    "public.funding_interest_payments",
    "public.funding_trades",
    "public.ledger_observation",
    "public.ledger_observation_query",
    "public.nav_peak",
    "public.nav_window_samples",
    "public.offer_claims",
    "public.position_state",
    "public.projection_heads",
    "public.quarantine_opening",
    "public.reconcile_observation",
    "public.sim_venue_event",
    "public.submission_attempt_journal",
    "public.submission_attempts",
    "public.trading_control_requests",
    "public.trading_state",
    "public.uncertainty_resolution_requests",
    "public.venue_credit_mirror",
    "public.venue_credit_state",
    "public.venue_offer_mirror",
    "public.venue_offer_state",
)


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _derived_realms() -> list[object]:
    """Every distinct realm value held by any realm table (NULL included, to refuse it)."""
    union = " UNION ".join(f"SELECT deployment_environment AS realm FROM {name}" for name in REALM_TABLES)
    rows = op.get_bind().execute(text(f"SELECT realm FROM ({union}) AS found ORDER BY realm NULLS FIRST"))
    return [row[0] for row in rows]


def upgrade() -> None:
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "database_realm",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("realm", sa.Text(), nullable=False),
        sa.Column("stamped_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.CheckConstraint("id", name="ck_database_realm_singleton"),
        sa.CheckConstraint("realm IN ('prod', 'shadow', 'ci')", name="ck_database_realm_realm"),
        sa.CheckConstraint("actor <> ''", name="ck_database_realm_actor"),
        sa.PrimaryKeyConstraint("id"),
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute(f"""CREATE FUNCTION public.{_MUTATION_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable database realm stamp'; END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_MUTATION_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER immutable_database_realm_write BEFORE UPDATE OR DELETE ON public.{_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_MUTATION_FUNCTION}()"
    )
    op.execute(
        f"CREATE TRIGGER immutable_database_realm_truncate BEFORE TRUNCATE ON public.{_TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION public.{_MUTATION_FUNCTION}()"
    )

    # SECURITY DEFINER: the writing role needs no privilege on the stamp.
    op.execute(f"""CREATE FUNCTION public.{_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE stamp text;
        BEGIN
          SELECT r.realm INTO stamp FROM public.{_TABLE} r;
          IF stamp IS NULL THEN
            RAISE EXCEPTION 'database realm is not stamped: refusing a write to %', TG_TABLE_NAME;
          END IF;
          IF NEW.deployment_environment IS DISTINCT FROM stamp THEN
            RAISE EXCEPTION 'database realm % refuses a write of realm % to %',
              stamp, NEW.deployment_environment, TG_TABLE_NAME;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_FUNCTION}() FROM PUBLIC")
    for name in REALM_TABLES:
        op.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE OF deployment_environment "
            f"ON {name} FOR EACH ROW EXECUTE FUNCTION public.{_FUNCTION}()"
        )

    realms = _derived_realms()
    if len(realms) > 1:
        raise RuntimeError(
            f"cannot stamp database_realm: realm tables hold more than one realm {realms!r}; "
            "one database holds one realm"
        )
    if realms:
        realm = realms[0]
        if realm not in _REALMS:
            raise RuntimeError(f"cannot stamp database_realm: realm tables hold {realm!r}")
        op.execute(
            text(
                f"INSERT INTO public.{_TABLE} (realm, stamped_at_ms, actor) "
                "VALUES (:realm, (extract(epoch FROM clock_timestamp()) * 1000)::bigint, :actor)"
            ).bindparams(realm=realm, actor=f"migration {revision}")
        )

    listed = ", ".join(_COLUMNS)
    op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_TABLE} FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM {role}")
            op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_TABLE} FROM {role}")
    if _role_exists("bfx_bot"):
        op.execute(f"GRANT SELECT ON public.{_TABLE} TO bfx_bot")


def downgrade() -> None:
    for name in REALM_TABLES:
        op.execute(f"DROP TRIGGER {_TRIGGER} ON {name}")
    op.execute(f"DROP FUNCTION public.{_FUNCTION}()")
    op.execute(f"DROP TRIGGER immutable_database_realm_truncate ON public.{_TABLE}")
    op.execute(f"DROP TRIGGER immutable_database_realm_write ON public.{_TABLE}")
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.drop_table("database_realm")
    # END AUTOGEN-DDL-DOWNGRADE
    op.execute(f"DROP FUNCTION public.{_MUTATION_FUNCTION}()")
