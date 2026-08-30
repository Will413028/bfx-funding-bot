"""persist immutable reservation decision correlation on offer claims.

Revision ID: f5b8d0e2f3a4
Revises: f4a7c9d1e2b3
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5b8d0e2f3a4"
down_revision: str | Sequence[str] | None = "f4a7c9d1e2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows predate audited reservation references. Leave them NULL so
    # replay/correlation treats them as explicit legacy data, never fabricated.
    op.add_column("offer_claims", sa.Column("execution_decision_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("offer_claims", "execution_decision_id")
