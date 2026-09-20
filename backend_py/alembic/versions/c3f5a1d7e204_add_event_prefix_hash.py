"""Bind each event to the rolling hash of the prefix it completes."""
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import op

revision = "c3f5a1d7e204"
down_revision = "b4e6f8a0c203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_log", sa.Column("prefix_hash", sa.Text(), nullable=True))
    # Seal every existing row here. A reader treats a missing hash as unproven
    # rather than absent, and the writer refuses to extend a chain whose
    # predecessor has none, so a partial backfill would block the next append.
    # The hash is derived with the same record contract the writer uses; deriving
    # it in SQL would create a second definition that could drift from it.
    from bfx_funding_bot.modules.execution.event_store.canonical import (
        GENESIS_PREFIX_HASH,
        rolling_prefix_hash,
    )
    from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

    session = Session(bind=op.get_bind())
    rows = session.scalars(
        select(EventLogRow).order_by(
            EventLogRow.exchange_account_id,
            EventLogRow.deployment_environment,
            EventLogRow.event_seq,
        )
    ).all()
    previous_key = None
    previous_hash = GENESIS_PREFIX_HASH
    for row in rows:
        key = (row.exchange_account_id, row.deployment_environment)
        if key != previous_key:
            previous_key, previous_hash = key, GENESIS_PREFIX_HASH
        previous_hash = rolling_prefix_hash(previous_hash, row)
        row.prefix_hash = previous_hash
    session.flush()
    session.commit()


def downgrade() -> None:
    op.drop_column("event_log", "prefix_hash")
