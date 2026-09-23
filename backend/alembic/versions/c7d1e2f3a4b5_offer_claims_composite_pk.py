"""offer_claims composite PK (account_id, deployment_environment, cid)

Revision ID: c7d1e2f3a4b5
Revises: 4f8c2e91b3a7
Create Date: 2026-05-24 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "4f8c2e91b3a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the now-redundant index (composite PK prefix covers acct+env lookups).
    op.drop_index("idx_offer_claims_acct_env", table_name="offer_claims")
    # Swap single-col PK (cid) -> composite (account_id, deployment_environment, cid).
    op.drop_constraint("offer_claims_pkey", "offer_claims", type_="primary")
    op.create_primary_key(
        "offer_claims_pkey",
        "offer_claims",
        ["account_id", "deployment_environment", "cid"],
    )


def downgrade() -> None:
    op.drop_constraint("offer_claims_pkey", "offer_claims", type_="primary")
    op.create_primary_key("offer_claims_pkey", "offer_claims", ["cid"])
    op.create_index(
        "idx_offer_claims_acct_env",
        "offer_claims",
        ["account_id", "deployment_environment"],
    )
