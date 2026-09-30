"""Link R6 quarantines to their source attempts, once per attempt.

Revision ID: a7b8c9d0e1f3
Revises: f6a7b8c9d0e1
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a7b8c9d0e1f3"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_CHECK = "ck_accepted_basis_attempt_classification"
_FK = "fk_quarantine_opening_source_attempt"
_INDEX = "uq_quarantine_opening_source_attempt"
_SOURCE_GUARD = "guard_ledger_quarantine_source"


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    op.drop_constraint(_CHECK, "accepted_capital_basis_attempt", type_="check")
    op.create_check_constraint(
        _CHECK,
        "accepted_capital_basis_attempt",
        "classification IN ('reflected','settled','unresolved','quarantined')",
    )
    op.add_column(
        "quarantine_opening", sa.Column("source_attempt_id", postgresql.UUID(), nullable=True)
    )
    op.create_foreign_key(
        _FK,
        "quarantine_opening",
        "submission_attempt_journal",
        ["source_attempt_id"],
        ["attempt_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        _INDEX,
        "quarantine_opening",
        ["source_attempt_id"],
        unique=True,
        postgresql_where=sa.text("source_attempt_id IS NOT NULL"),
    )
    op.execute(f"""CREATE FUNCTION public.{_SOURCE_GUARD}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF NEW.source_attempt_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.submission_attempt_journal a
            WHERE a.attempt_id = NEW.source_attempt_id
              AND (a.exchange_account_id, a.deployment_environment, a.symbol)
                  IS DISTINCT FROM
                  (NEW.exchange_account_id, NEW.deployment_environment, NEW.symbol)
          ) THEN
            RAISE EXCEPTION 'ledger quarantine source scope mismatch';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_SOURCE_GUARD}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {_SOURCE_GUARD} BEFORE INSERT ON public.quarantine_opening "
        f"FOR EACH ROW EXECUTE FUNCTION public.{_SOURCE_GUARD}()"
    )
    op.execute("REVOKE ALL (source_attempt_id) ON TABLE public.quarantine_opening FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(
                f"REVOKE ALL (source_attempt_id) ON TABLE public.quarantine_opening FROM {role}"
            )
    if _role_exists("bfx_cutover_reader"):
        op.execute(
            "GRANT SELECT (source_attempt_id) ON public.quarantine_opening TO bfx_cutover_reader"
        )
    # b1e2d3a4c5f6 grants bfx_bot table-level SELECT/INSERT on this table;
    # those privileges already cover the new column (no INSERT column list).


def downgrade() -> None:
    # Immutable facts cannot be rewritten to fit the older contract.
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM public.accepted_capital_basis_attempt "
                "WHERE classification = 'quarantined') OR EXISTS "
                "(SELECT 1 FROM public.quarantine_opening WHERE source_attempt_id IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("refuse downgrade with R6 quarantine facts")
    op.execute(f"DROP TRIGGER {_SOURCE_GUARD} ON public.quarantine_opening")
    op.execute(f"DROP FUNCTION public.{_SOURCE_GUARD}()")
    op.drop_index(_INDEX, table_name="quarantine_opening")
    op.drop_constraint(_FK, "quarantine_opening", type_="foreignkey")
    op.drop_column("quarantine_opening", "source_attempt_id")
    op.drop_constraint(_CHECK, "accepted_capital_basis_attempt", type_="check")
    op.create_check_constraint(
        _CHECK,
        "accepted_capital_basis_attempt",
        "classification IN ('reflected','settled','unresolved')",
    )
