"""Drop ``deployments.change_class``.

Revises 8e4b2f6a1c37. Plan docs/superpowers/plans/2026-09-25-lending-envelope.md
§5: the follow-up release. 1f6392809120 made the column nullable while the
previous bfx-deploy still wrote it; every attempt since has been written by the
current tool, which leaves it NULL (ledger rows 7-10 on production).

No information is lost: every row that carries a class also names it in
``detail`` (``class=material(...)``), which the previous tool always wrote.

``check_deployment_attempt`` pairs the started and terminal rows of an attempt;
it is recreated without the column in the same step, otherwise every insert
would fail on ``opened.change_class``.

Downgrade re-adds the column nullable (the 1f6392809120 shape) and the old
pairing check. The dropped values are not restored: the ledger is append-only,
and ``detail`` keeps them.
"""
import sqlalchemy as sa

from alembic import op

revision = "5b9e3d7a2f41"
down_revision = "8e4b2f6a1c37"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"


def _pairing(columns: str) -> None:
    op.execute(f"""CREATE OR REPLACE FUNCTION public.check_deployment_attempt() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE opened public.deployments%ROWTYPE; BEGIN
          SELECT * INTO opened FROM public.deployments
            WHERE attempt_id = NEW.attempt_id AND outcome = 'started';
          IF NEW.outcome = 'started' THEN
            IF EXISTS (SELECT FROM public.deployments WHERE attempt_id = NEW.attempt_id) THEN
              RAISE EXCEPTION 'deployment attempt % already recorded', NEW.attempt_id;
            END IF;
          ELSIF FOUND AND ({", ".join(f"opened.{c}" for c in columns.split())})
                IS DISTINCT FROM ({", ".join(f"NEW.{c}" for c in columns.split())}) THEN
            RAISE EXCEPTION 'deployment attempt % finished as a different release', NEW.attempt_id;
          END IF;
          RETURN NEW; END $$""")


def upgrade() -> None:
    # The function's %ROWTYPE resolves at call time; replace it before the drop so
    # no insert can see the old body against the new row type.
    _pairing("source_revision backend_digest frontend_digest started_at")
    op.drop_column("deployments", "change_class")


def downgrade() -> None:
    op.add_column("deployments", sa.Column("change_class", sa.Text(), nullable=True))
    _pairing("source_revision backend_digest frontend_digest change_class started_at")
