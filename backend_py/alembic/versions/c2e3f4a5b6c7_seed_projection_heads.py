"""Seed serialized-projector cursors from the pre-cutover event history.

Revision ``bc4d5e6f7081`` introduced ``projection_heads`` alongside the
normalized execution tables.  Existing snapshots were already maintained by
the legacy event-store writer, so starting those accounts at cursor zero would
replay the entire history and double-count position deltas.  This forward-only
backfill marks the historical stream as consumed; any rows written after the
cutover are then handled by ``AccountEventWriter``'s normal gap replay.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "bc4d5e6f7081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Initialize one execution-state cursor per account/environment stream."""
    op.execute(
        sa.text(
            "INSERT INTO projection_heads ("
            "  exchange_account_id, deployment_environment, projection_name, "
            "  last_event_seq, projector_version"
            ") "
            "SELECT exchange_account_id, deployment_environment, "
            "  'execution_state', max(event_seq), 'legacy-v2' "
            "FROM event_log "
            "WHERE exchange_account_id IS NOT NULL "
            "GROUP BY exchange_account_id, deployment_environment "
            "ON CONFLICT (exchange_account_id, deployment_environment, projection_name) "
            "DO NOTHING"
        )
    )


def downgrade() -> None:
    """Remove only cursors created by this historical backfill."""
    op.execute(
        sa.text(
            "DELETE FROM projection_heads "
            "WHERE projection_name = 'execution_state' "
            "AND projector_version = 'legacy-v2'"
        )
    )
