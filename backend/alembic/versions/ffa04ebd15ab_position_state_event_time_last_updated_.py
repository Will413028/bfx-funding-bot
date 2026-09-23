"""position_state event-time last_updated_ms (drop wall-clock updated_at)

Revision ID: ffa04ebd15ab
Revises: e9f4a2b1c8d7
Create Date: 2026-05-27 21:24:01.713655

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ffa04ebd15ab'
down_revision: str | Sequence[str] | None = 'e9f4a2b1c8d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace wall-clock updated_at with event-time last_updated_ms.

    Expand-then-contract so existing rows survive the NOT NULL: add nullable,
    backfill from the event_log (the SoT), then enforce NOT NULL and drop the
    old column.

    The backfill MUST mirror the runtime projection fold (store.py
    _project_position_state): each event assigns last_updated_ms = its own
    occurred_at_ms in event_seq order, so the converged value is the
    occurred_at_ms of the HIGHEST-event_seq event — not max(occurred_at_ms).
    They differ only when event time is non-monotonic vs event_seq (e.g. a
    WS event interleaved with a reconcile on a different clock); using the
    last-by-seq rule keeps backfill == rebuild_snapshot_from_log, preserving
    the determinism guarantee.
    """
    op.add_column('position_state', sa.Column('last_updated_ms', sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE position_state ps
        SET last_updated_ms = COALESCE(
            (SELECT el.occurred_at_ms FROM event_log el
             WHERE el.account_id = ps.account_id
               AND el.deployment_environment = ps.deployment_environment
             ORDER BY el.event_seq DESC
             LIMIT 1),
            0)
        """
    )
    op.alter_column('position_state', 'last_updated_ms', nullable=False)
    op.drop_column('position_state', 'updated_at')


def downgrade() -> None:
    """Restore the legacy wall-clock column.

    NOTE: this intentionally re-creates the original frozen-timestamp behavior
    (server_default, no onupdate) and resets existing rows to downgrade-time
    wall-clock — the event-time history in last_updated_ms is NOT recoverable.
    """
    op.add_column('position_state', sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), autoincrement=False, nullable=False))
    op.drop_column('position_state', 'last_updated_ms')
