"""`deployments`: one append-only row per bfx-deploy attempt that found a target.

Written only by the host tool (deploy/vm/ops/bfx_deploy.py) through `docker exec
bfx-postgres psql` as the owner role; the runtime roles may only read it. The
row records what was attempted and how it ended -- source revision, both image
digests, change class, whether migrations ran, outcome -- and is never updated:
a trigger rejects UPDATE, DELETE and TRUNCATE. Operator approval of a material
release is a separate append-only fact (T5), not a later edit of this row.

The CHECK constraints (hex revision, sha256 digests, class/outcome vocabularies,
bounded detail) live in the migration only: they use PostgreSQL regex syntax and
Alembic does not compare CHECK constraints, while this metadata must stay
creatable on the SQLite unit-test engine.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

DEPLOYMENT_OUTCOMES = ("deployed", "rolled_back", "failed")
CHANGE_CLASSES = ("standard", "material")


class DeploymentRow(Base):
    __tablename__ = "deployments"

    # Ordering is by id: the newest row with outcome 'deployed' is the running
    # release; timestamps are the host's clock and only describe the attempt.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_revision: Mapped[str] = mapped_column(Text, nullable=False)
    backend_digest: Mapped[str] = mapped_column(Text, nullable=False)
    frontend_digest: Mapped[str] = mapped_column(Text, nullable=False)
    change_class: Mapped[str] = mapped_column(Text, nullable=False)
    migrations_applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    # The CI run that built the images, when known; GHCR images carry no run id yet.
    ci_run: Mapped[str | None] = mapped_column(Text, nullable=True)
