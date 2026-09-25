"""Retire ``deployments.change_class``: nullable, no CHECK.

Revises c74d45a54e46 (deployment ledger). Plan
docs/superpowers/plans/2026-09-25-lending-envelope.md D5/T6: change classes are
gone, and the new bfx-deploy no longer writes the column.

The column is not dropped yet. This release is deployed by the previous
bfx-deploy (host tooling is installed from a release only after it deployed),
which still inserts ``change_class`` into both ledger rows of its attempt --
possibly after this migration ran. So the column stays, accepting whatever that
tool writes, and becomes NULL for every row the new tool appends. The follow-up
release drops it.

``check_deployment_attempt`` still pairs ``change_class`` between the started
and terminal rows with ``IS DISTINCT FROM``, which treats two NULLs as equal:
each tool writes both rows of its own attempt the same way, so the trigger needs
no change. Grants are unchanged (the runtime roles keep SELECT on the table).

Downgrade restores the CHECK and NOT NULL. Rows the new tool wrote carry no
class; they become ``material``, which is what the previous tool records for a
release it cannot classify (both rows of an attempt alike, so pairs stay
paired). That is the one write this ledger ever takes besides an append, so the
append-only trigger is disabled for exactly that statement.
"""
from alembic import op

revision = "1f6392809120"
down_revision = "c74d45a54e46"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"


def upgrade() -> None:
    op.drop_constraint("ck_deployments_change_class", "deployments", type_="check")
    op.alter_column("deployments", "change_class", nullable=True)


def downgrade() -> None:
    op.execute("ALTER TABLE public.deployments DISABLE TRIGGER deployments_append_only")
    op.execute("UPDATE public.deployments SET change_class = 'material' WHERE change_class IS NULL")
    op.execute("ALTER TABLE public.deployments ENABLE TRIGGER deployments_append_only")
    op.alter_column("deployments", "change_class", nullable=False)
    op.create_check_constraint("ck_deployments_change_class", "deployments",
                               "change_class IN ('standard', 'material')")
