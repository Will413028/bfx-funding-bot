"""The simulated venue's durable event log: ``sim_venue_event``, insert-only.

One migration chain reaches every database, so the table exists (empty) in the
production database too. It cannot hold a production row: the realm CHECK allows only
``shadow`` and ``ci`` (ADR 2026-10-03 D1; the later ``database_realm`` row supersedes
this CHECK without conflicting with it).

The table is not a ledger table and does not join the dormancy triggers: the venue is an
independent counterparty, not gated on the bot's authority epoch. Its own store checks
the epoch on open.

Insert-only: a row trigger rejects UPDATE and DELETE, a statement trigger TRUNCATE
(``reject_sim_venue_mutation``, ``search_path = pg_catalog``, EXECUTE revoked from PUBLIC).

Grants: default privileges in a host's database may hand every runtime role DML on a
new table, so this revokes everything from PUBLIC and from ``bfx_bot``, ``bfx_webapi``,
``bfx_webauth`` and ``bfx_cutover_reader``, then grants ``bfx_bot`` SELECT and INSERT
only. The web API and the cutover reader hold nothing (the webapi exact allowlist
stays as it is). The table has no sequence: positions are assigned by the writer.

The downgrade refuses a database holding events: the log is durable venue state.

Revision ID: e6b1d4a7c9f3
Revises: a3b4c5d6e7f8
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e6b1d4a7c9f3"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_TABLE = "sim_venue_event"
_FUNCTION = "reject_sim_venue_mutation"
_COLUMNS = (
    "exchange_account_id", "deployment_environment", "seq", "event_type",
    "schema_version", "payload", "recorded_at",
)
_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "sim_venue_event",
        sa.Column("exchange_account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("exchange_account_id <> ''", name="ck_sim_venue_event_account"),
        sa.CheckConstraint(
            "deployment_environment IN ('shadow', 'ci')", name="ck_sim_venue_event_realm"
        ),
        sa.CheckConstraint("schema_version >= 1", name="ck_sim_venue_event_version"),
        sa.CheckConstraint("seq >= 1", name="ck_sim_venue_event_seq"),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "seq", name="pk_sim_venue_event"
        ),
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute(f"""CREATE FUNCTION public.{_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable simulated venue event log'; END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER immutable_sim_venue_write BEFORE UPDATE OR DELETE ON public.{_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_FUNCTION}()"
    )
    op.execute(
        f"CREATE TRIGGER immutable_sim_venue_truncate BEFORE TRUNCATE ON public.{_TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION public.{_FUNCTION}()"
    )

    listed = ", ".join(_COLUMNS)
    op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_TABLE} FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM {role}")
            op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_TABLE} FROM {role}")
    if _role_exists("bfx_bot"):
        op.execute(f"GRANT SELECT, INSERT ON public.{_TABLE} TO bfx_bot")


def downgrade() -> None:
    if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{_TABLE})")).scalar():
        raise RuntimeError(f"{_TABLE} holds simulated venue events; refusing to drop the log")
    op.execute(f"DROP TRIGGER immutable_sim_venue_truncate ON public.{_TABLE}")
    op.execute(f"DROP TRIGGER immutable_sim_venue_write ON public.{_TABLE}")
    op.drop_table(_TABLE)
    op.execute(f"DROP FUNCTION public.{_FUNCTION}()")
