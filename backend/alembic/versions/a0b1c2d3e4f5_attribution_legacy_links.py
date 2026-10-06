"""Weekly attribution: copy the legacy links it needs out of the frozen legacy tables (S1-8 D4a).

The weekly attribution recomputes every week from all history, so what the legacy authority
recorded about pre-switch offers and credits is a permanent input. Since the authority switch
those tables are frozen (``e8f9a0b1c2d3``) and S1-8 moves them out of ``public``; this revision
copies, once, exactly what the weekly derived from them into attribution's own tables, and the
loader reads only those:

* ``attribution_legacy_offer_links`` -- per venue offer the execution decision / signal
  correlation id each legacy source named: ``offer_claims`` (rows with a venue offer id),
  ``venue_offer_state``, and the ``ORDER_FILL`` events of ``event_log`` (offer id from the
  payload, else the column; signal correlation id from the payload). The weekly resolves them to
  a cell through ``execution_decisions`` / ``diagnostics`` (shared tables, not frozen) as
  before. Distinct per source; a link without a venue offer id is dropped (no funding trade has
  an empty offer id, so it never matched);
* ``attribution_legacy_open_credits`` -- the non-terminal ``venue_credit_state`` rows with a
  rate, period and creation time (the weekly skipped the others).

Rows of a NULL ``exchange_account_id`` (legacy-only test realms) are not copied: the weekly
reads by the account UUID only. The copy is deterministic and idempotent (unique keys, ``ON
CONFLICT DO NOTHING``) and a no-op when the legacy tables are empty (CI, a fresh host). The
fill extraction runs in Python with the same expressions the loader used, so JSON values
convert exactly as before.

Grants: owner-written only. ``bfx_bot`` (the weekly job's role) gets SELECT; ``bfx_webapi``,
``bfx_webauth`` and ``bfx_cutover_reader`` nothing (the web API's allowlist is unchanged). Both
tables carry the realm guard like every ``deployment_environment`` table.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
"""

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "a0b1c2d3e4f5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

LINKS = "attribution_legacy_offer_links"
OPEN_CREDITS = "attribution_legacy_open_credits"
# Every table with a ``deployment_environment`` column this revision adds; the realm coverage
# test adds them to ``a7c3e9f1b2d4``'s list.
REALM_TABLES = (f"public.{LINKS}", f"public.{OPEN_CREDITS}")
_READER = "bfx_bot"
_NO_ACCESS = ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_SOURCES = ("claim", "venue_offer", "fill")


def _role_exists(conn: Connection, name: str) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name}).scalar()
    )


def _fill_link(payload_offer: Any, column_offer: Any, payload_scid: Any) -> tuple[str, str | None]:
    """The (venue offer id, signal correlation id) the loader took from one ORDER_FILL."""
    return str(payload_offer or column_offer or ""), str(payload_scid or "") or None


def _links(conn: Connection) -> Iterable[tuple[Any, str, str, str, str | None, str | None]]:
    for scope, env, offer, decision, scid in conn.execute(text(
        "SELECT exchange_account_id, deployment_environment, venue_offer_id, "
        "execution_decision_id, signal_correlation_id FROM public.offer_claims "
        "WHERE exchange_account_id IS NOT NULL AND venue_offer_id IS NOT NULL"
    )):
        yield scope, env, "claim", str(offer), decision, scid
    for scope, env, offer, decision, scid in conn.execute(text(
        "SELECT exchange_account_id, deployment_environment, venue_offer_id, "
        "execution_decision_id, signal_correlation_id FROM public.venue_offer_state"
    )):
        yield scope, env, "venue_offer", offer, decision, scid
    for scope, env, payload_offer, column_offer, payload_scid in conn.execute(text(
        "SELECT exchange_account_id, deployment_environment, payload->'venue_offer_id', "
        "venue_offer_id, payload->'signal_correlation_id' FROM public.event_log "
        "WHERE event_type = 'ORDER_FILL' AND exchange_account_id IS NOT NULL"
    )):
        offer, scid = _fill_link(payload_offer, column_offer, payload_scid)
        yield scope, env, "fill", offer, None, scid


def _order(value: str | None) -> tuple[bool, str]:
    return value is not None, value or ""


def materialize(conn: Connection) -> None:
    """Copy the legacy links and open credits (idempotent; owner connection)."""
    links = sorted(
        {link for link in _links(conn) if link[3]},
        key=lambda r: (str(r[0]), r[1], _SOURCES.index(r[2]), r[3], _order(r[4]), _order(r[5])),
    )
    if links:
        conn.execute(
            text(
                f"INSERT INTO public.{LINKS} (exchange_account_id, deployment_environment, "
                "source, venue_offer_id, execution_decision_id, signal_correlation_id) "
                "VALUES (:scope, :env, :source, :offer, :decision, :scid) "
                f"ON CONFLICT ON CONSTRAINT uq_{LINKS} DO NOTHING"
            ),
            [
                {"scope": scope, "env": env, "source": source, "offer": offer,
                 "decision": decision, "scid": scid}
                for scope, env, source, offer, decision, scid in links
            ],
        )
    conn.execute(text(
        f"INSERT INTO public.{OPEN_CREDITS} (exchange_account_id, deployment_environment, "
        "credit_id, symbol, amount, rate, period_days, mts_created) "
        "SELECT exchange_account_id, deployment_environment, credit_id, symbol, amount, rate, "
        "period, mts_created FROM public.venue_credit_state "
        "WHERE NOT is_terminal AND rate IS NOT NULL AND period IS NOT NULL "
        "AND mts_created IS NOT NULL "
        "ORDER BY exchange_account_id, deployment_environment, credit_id "
        "ON CONFLICT DO NOTHING"
    ))


def upgrade() -> None:
    op.create_table(
        LINKS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("execution_decision_id", sa.Text(), nullable=True),
        sa.Column("signal_correlation_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("source IN ('claim', 'venue_offer', 'fill')",
                           name=f"ck_{LINKS}_source"),
        sa.CheckConstraint("venue_offer_id <> ''", name=f"ck_{LINKS}_offer"),
        sa.UniqueConstraint(
            "exchange_account_id", "deployment_environment", "source", "venue_offer_id",
            "execution_decision_id", "signal_correlation_id",
            name=f"uq_{LINKS}", postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        OPEN_CREDITS,
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("credit_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("mts_created", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("exchange_account_id", "deployment_environment", "credit_id"),
    )
    conn = op.get_bind()
    for name in REALM_TABLES:
        op.execute(
            f"CREATE TRIGGER database_realm_write BEFORE INSERT OR UPDATE OF "
            f"deployment_environment ON {name} FOR EACH ROW "
            "EXECUTE FUNCTION public.guard_database_realm()"
        )
    for table in (LINKS, OPEN_CREDITS):
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        for role in _NO_ACCESS:
            if _role_exists(conn, role):
                op.execute(f"REVOKE ALL ON public.{table} FROM {role}")
        if _role_exists(conn, _READER):
            op.execute(f"REVOKE ALL ON public.{table} FROM {_READER}")
            op.execute(f"GRANT SELECT ON public.{table} TO {_READER}")
    for role in (_READER, *_NO_ACCESS):
        if _role_exists(conn, role):
            op.execute(f"REVOKE ALL ON SEQUENCE public.{LINKS}_id_seq FROM {role}")
    materialize(conn)


def downgrade() -> None:
    op.drop_table(OPEN_CREDITS)
    op.drop_table(LINKS)
