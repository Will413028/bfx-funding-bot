"""Record the rolling prefix hash of each event beside the append-only ledger."""
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import op

revision = "c3f5a1d7e204"
down_revision = "b4e6f8a0c203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Beside the ledger, not on it. event_log is append-only and
    # guard_capital_event() enforces that for capital-bearing rows, so sealing
    # existing events with an UPDATE is refused for exactly the events capital
    # reads depend on. Inserting keeps the ledger immutable and the chain whole.
    op.create_table(
        "event_prefix_hashes",
        sa.Column("event_seq", sa.BigInteger(),
                  sa.ForeignKey("event_log.event_seq", ondelete="RESTRICT"), primary_key=True),
        sa.Column("exchange_account_id", sa.Uuid(), nullable=True),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("prefix_hash", sa.Text(), nullable=False),
    )
    op.create_index("idx_event_prefix_scope_seq", "event_prefix_hashes",
                    ["exchange_account_id", "deployment_environment", "event_seq"])

    # Seal every existing event here. A reader treats a missing link as unproven
    # rather than absent, and the writer refuses to extend a chain whose
    # predecessor has none, so a partial backfill would block the next append.
    # The hash is derived with the same record contract the writer uses; deriving
    # it in SQL would create a second definition that could drift from it.
    from bfx_funding_bot.modules.execution.event_store.canonical import (
        GENESIS_PREFIX_HASH,
        rolling_prefix_hash,
    )
    from bfx_funding_bot.modules.execution.event_store.tables import (
        EventLogRow,
        EventPrefixHashRow,
    )

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
        session.add(EventPrefixHashRow(
            event_seq=row.event_seq,
            exchange_account_id=row.exchange_account_id,
            deployment_environment=row.deployment_environment,
            prefix_hash=previous_hash,
        ))
    session.flush()


def downgrade() -> None:
    op.drop_index("idx_event_prefix_scope_seq", table_name="event_prefix_hashes")
    op.drop_table("event_prefix_hashes")
