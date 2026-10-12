"""auth."twoFactor": the account-lockout columns Better Auth 1.6.22+ reads and writes.

The twoFactor plugin counts failed second-factor verifications per account and locks the
account once the budget is spent. It reads ``lockedUntil`` before every verification and
writes both columns after every success (reset) and every failure (increment), so without
them every 2FA sign-in fails, not only the failing ones.

``failedVerificationCount`` defaults to 0 so existing rows start with an empty budget;
``lockedUntil`` is NULL (not locked). Both are nullable, as the plugin declares them
(``required: false``). ``bfx_webauth`` holds table-level SELECT/INSERT/UPDATE/DELETE on
``auth`` tables, which covers new columns; nothing is granted here.

Compatibility: Better Auth 1.6.14 (the running frontend until this release) never names
these columns; inserts get the defaults. No public table changes.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md): dropping the columns would
discard lockout state.

Revision ID: 49b37faec086
Revises: 2e835b6f4c12
"""

import sqlalchemy as sa

from alembic import op

revision = "49b37faec086"
down_revision = "2e835b6f4c12"
branch_labels = None
depends_on = None
# Auth schema only; no projection table, cursor or event_log content changes.
ledger_contract = "preserved"


def upgrade() -> None:
    op.add_column(
        "twoFactor",
        sa.Column("failedVerificationCount", sa.Integer(), nullable=True, server_default="0"),
        schema="auth",
    )
    op.add_column(
        "twoFactor",
        sa.Column("lockedUntil", sa.DateTime(timezone=True), nullable=True),
        schema="auth",
    )


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
