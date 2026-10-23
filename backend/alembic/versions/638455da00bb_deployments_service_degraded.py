"""The deployments ledger records whether an attempt left production degraded; the weekly report reads it.

Delivery metrics (change failure rate, failed deployment recovery time) need to tell a
``failed`` attempt that never touched production (a precheck, a restore test before
anything stopped) from one that left the bot or the web API stopped or unhealthy. The
``detail`` text cannot carry that reliably, so bfx-deploy records it:

- ``service_degraded``: on a terminal row, whether the attempt ended with the bot or the web
  API stopped or unhealthy (``Attempt.degraded`` in ``deploy/vm/ops/bfx_deploy.py``). NULL on
  ``started`` rows and on every row written before this revision; the report counts only
  rows that carry a value.
- ``bfx_research`` gets SELECT on ``deployments`` (read only; the table holds revisions,
  digests and outcomes, no secret), so ``scripts/report_delivery_metrics.py`` runs in the
  weekly report under the role the other steps use.

No row is rewritten. ``check_deployment_attempt`` compares named columns only, so it is
unchanged. The previous bfx-deploy does not write the column and keeps working on this
schema (its rows read NULL); the deployer that writes it is installed only after this
release deployed.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 638455da00bb
Revises: 3c5e7a9b1d2f
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "638455da00bb"
down_revision = "3c5e7a9b1d2f"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_ROLE = "bfx_research"


def upgrade() -> None:
    op.add_column("deployments", sa.Column("service_degraded", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_deployments_service_degraded", "deployments",
        "outcome <> 'started' OR service_degraded IS NULL",
    )
    if op.get_bind().execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"),
                             {"name": _ROLE}).scalar():
        op.execute(f"GRANT SELECT ON public.deployments TO {_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
