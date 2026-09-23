"""add versioned fill-model artifacts

Revision ID: f5b8d0e2f3c4
Revises: a6c9e2f4b7d1
Create Date: 2026-08-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5b8d0e2f3c4"
down_revision: str | Sequence[str] | None = "a6c9e2f4b7d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fill_rate_model_artifacts",
        sa.Column("artifact_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("period_agg", sa.Text(), nullable=False),
        sa.Column("horizon_h", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("training_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("training_end_ms", sa.BigInteger(), nullable=False),
        sa.Column("cutoff_ms", sa.BigInteger(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("confidence_min_samples", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("artifact_hash"),
    )
    op.add_column(
        "fill_rate_stats",
        sa.Column("artifact_hash", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_fill_rate_stats_artifact_hash",
        "fill_rate_stats",
        "fill_rate_model_artifacts",
        ["artifact_hash"],
        ["artifact_hash"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_fill_rate_stats_artifact_hash", "fill_rate_stats", type_="foreignkey")
    op.drop_column("fill_rate_stats", "artifact_hash")
    op.drop_table("fill_rate_model_artifacts")
