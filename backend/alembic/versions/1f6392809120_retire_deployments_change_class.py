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

Downgrade restores the CHECK and NOT NULL, and refuses while any row carries no
class: inventing one would write a classification that never happened into an
append-only ledger.
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
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.deployments WHERE change_class IS NULL) THEN
          RAISE EXCEPTION 'refuse downgrade: deployments recorded without a change class';
        END IF; END $$""")
    op.alter_column("deployments", "change_class", nullable=False)
    op.create_check_constraint("ck_deployments_change_class", "deployments",
                               "change_class IN ('standard', 'material')")
