"""Record the command-clock revision each quarantine was opened at.

A quarantine opened after an accepted basis is exactly one whose
``opened_revision`` exceeds the basis's ``accept_revision``, so the capital
reader and the basis writer read ``basis quarantines + opened_revision > accept``
through a scoped index instead of anti-joining every quarantine of the scope.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
# Columns this revision adds to existing tables (all readable by the reader group).
_ADDED = {"quarantine_opening": ("opened_revision",)}
_POPULATED_GUARD = ("quarantine_opening",)


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _refuse_populated(direction: str) -> None:
    for name in _POPULATED_GUARD:
        if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{name})")).scalar():
            raise RuntimeError(f"refuse {direction} of populated ledger: {name}")


def upgrade() -> None:
    _refuse_populated("upgrade")
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.add_column(
        "quarantine_opening", sa.Column("opened_revision", sa.BigInteger(), nullable=False)
    )
    op.create_index(
        "ix_quarantine_opening_scope_revision",
        "quarantine_opening",
        ["exchange_account_id", "deployment_environment", "opened_revision"],
        unique=False,
    )
    # Autogenerate does not emit CHECK changes on existing tables.
    op.create_check_constraint(
        "ck_quarantine_opening_revision", "quarantine_opening", "opened_revision > 0"
    )
    # END AUTOGEN-DDL-UPGRADE

    for table, columns in _ADDED.items():
        listed = ", ".join(columns)
        op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{table} FROM PUBLIC")
        for role in _ROLES:
            if _role_exists(role):
                op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{table} FROM {role}")
        if _role_exists("bfx_cutover_reader"):
            op.execute(f"GRANT SELECT ({listed}) ON public.{table} TO bfx_cutover_reader")


def downgrade() -> None:
    _refuse_populated("downgrade")
    op.drop_constraint("ck_quarantine_opening_revision", "quarantine_opening", type_="check")
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.drop_index("ix_quarantine_opening_scope_revision", table_name="quarantine_opening")
    op.drop_column("quarantine_opening", "opened_revision")
    # END AUTOGEN-DDL-DOWNGRADE
