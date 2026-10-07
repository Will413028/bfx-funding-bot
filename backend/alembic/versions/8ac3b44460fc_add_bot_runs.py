"""Record each bot run, so the next boot can report one that ended without a word.

``bot_runs`` holds one row per bot process that booted as the writer
(``modules/observability/bot_runs.py``): inserted once the process holds the writer lock
and has been built, closed with ``clean_stop`` / ``fatal`` / ``boot_refused`` on the way
out. A run killed before it could say so (loop watchdog ``_exit``, OOM kill, SIGKILL,
segfault, host crash) leaves its row open; the next boot of the same scope alerts once
and closes it as ``unclean``. Not part of the capital ledger.

Grants: everything is revoked from PUBLIC and the runtime roles, then ``bfx_bot`` gets
SELECT, INSERT and UPDATE of the two end columns only (no DELETE: rows are never
pruned, one per restart). ``bfx_webapi``, ``bfx_webauth`` and ``bfx_cutover_reader`` get
nothing, so the web API allowlist is unchanged. The table carries the realm guard like
every ``deployment_environment`` table.

Additive: the previous web API image never reads it, and a bot image older than this
revision refuses the schema at boot as with any revision. The downgrade drops the table
and its rows (run history only).

Revision ID: 8ac3b44460fc
Revises: a6c7e8f9b0d1
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import op

revision = "8ac3b44460fc"
down_revision = "a6c7e8f9b0d1"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

TABLE = "bot_runs"
# Every table with a ``deployment_environment`` column this revision adds; the realm coverage
# test adds them to ``a7c3e9f1b2d4``'s list.
REALM_TABLES = (f"public.{TABLE}",)
END_COLUMNS = "end_reason, end_recorded_at_ms"
_WRITER = "bfx_bot"
_NO_ACCESS = ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader")


def _role_exists(conn: Connection, name: str) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name}).scalar()
    )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("exchange_account_id", sa.UUID(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("image_digest", sa.Text(), nullable=True),
        sa.Column("host_name", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("end_recorded_at_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "end_reason IN ('clean_stop', 'fatal', 'boot_refused', 'unclean')",
            name="ck_bot_runs_end_reason",
        ),
        sa.CheckConstraint(
            "(end_reason IS NULL) = (end_recorded_at_ms IS NULL)", name="ck_bot_runs_end_pair",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"], ["exchange_accounts.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_bot_runs_scope_started", TABLE,
        ["exchange_account_id", "deployment_environment", "started_at_ms"], unique=False,
    )
    conn = op.get_bind()
    for name in REALM_TABLES:
        op.execute(
            f"CREATE TRIGGER database_realm_write BEFORE INSERT OR UPDATE OF "
            f"deployment_environment ON {name} FOR EACH ROW "
            "EXECUTE FUNCTION public.guard_database_realm()"
        )
    op.execute(f"REVOKE ALL ON public.{TABLE} FROM PUBLIC")
    for role in _NO_ACCESS:
        if _role_exists(conn, role):
            op.execute(f"REVOKE ALL ON public.{TABLE} FROM {role}")
    if _role_exists(conn, _WRITER):
        op.execute(f"REVOKE ALL ON public.{TABLE} FROM {_WRITER}")
        op.execute(f"GRANT SELECT, INSERT ON public.{TABLE} TO {_WRITER}")
        op.execute(f"GRANT UPDATE ({END_COLUMNS}) ON public.{TABLE} TO {_WRITER}")


def downgrade() -> None:
    op.drop_index("ix_bot_runs_scope_started", table_name=TABLE)
    op.drop_table(TABLE)
