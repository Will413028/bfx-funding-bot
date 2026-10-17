"""The Bitfinex user id a credential belongs to, stored on the credential row.

``exchange_account_credentials.bitfinex_user_id`` (BIGINT, nullable; existing rows NULL).
The rotation worker compares a new key's account with the stored id of the credential it
replaces, so a key already revoked at Bitfinex (which can no longer answer
``v2/auth/r/info/user``) can still be replaced through the outbox. Written by an applied
rotation and the operator bootstrap; the bot fills a NULL active row once from the key it
booted on (trust on first use, ``modules/execution/credential_rotation.py``).

A CHECK keeps the value a positive id when present. Grants are unchanged: ``bfx_bot``'s and
``bfx_webapi``'s table-level privileges cover the new column, and ``bfx_operator``'s
column-level SELECT (``1db83367918c``) does not name it.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 1d34a3306e02
Revises: 1db83367918c
"""
import sqlalchemy as sa

from alembic import op

revision = "1d34a3306e02"
down_revision = "1db83367918c"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

TABLE = "exchange_account_credentials"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("bitfinex_user_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_exchange_account_credentials_bitfinex_user_id_positive", TABLE,
        "bitfinex_user_id IS NULL OR bitfinex_user_id > 0",
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
