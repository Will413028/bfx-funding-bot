"""`deployments`: append-only rows per bfx-deploy attempt that found a target.

Written only by the host tool (deploy/vm/ops/bfx_deploy.py) through `docker exec
bfx-postgres psql` as the owner role; the runtime roles may only read it. The
row records what was attempted and how it ended -- source revision, both image
digests, whether migrations ran, outcome -- and is never updated:
a trigger rejects UPDATE, DELETE and TRUNCATE. An attempt that changes the
containers has two rows sharing `attempt_id`: `started` (appended before the
containers are created; `attempt_id` is the BFX_DEPLOYMENT_ID the containers
get) and a terminal outcome; an attempt that stops earlier has only the latter.

The CHECK constraints mirror the migration (PostgreSQL is the authority);
the regex ones are created on PostgreSQL only, so SQLite fixtures still build.
Alembic does not compare CHECKs: tests/integration/test_model_constraints.py
does, against the migrated schema.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

DEPLOYMENT_STARTED = "started"
DEPLOYMENT_OUTCOMES = ("deployed", "rolled_back", "failed")
_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"


class DeploymentRow(Base):
    __tablename__ = "deployments"

    # Ordering is by id: the newest row with outcome 'deployed' is the running
    # release; timestamps are the host's clock and only describe the attempt.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL exactly on the `started` row.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_revision: Mapped[str] = mapped_column(Text, nullable=False)
    backend_digest: Mapped[str] = mapped_column(Text, nullable=False)
    frontend_digest: Mapped[str] = mapped_column(Text, nullable=False)
    migrations_applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    # The CI run that passed this revision (image label bfx.ci-run-url), when known.
    ci_run: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Regex CHECKs are PostgreSQL's (the authority); SQLite fixtures skip them.
        CheckConstraint("source_revision ~ '^[0-9a-f]{40}$'",
                        name="ck_deployments_source_revision").ddl_if(dialect="postgresql"),
        CheckConstraint(f"backend_digest ~ {_DIGEST_RE} AND frontend_digest ~ {_DIGEST_RE}",
                        name="ck_deployments_digests").ddl_if(dialect="postgresql"),
        CheckConstraint("outcome IN ('started', 'deployed', 'rolled_back', 'failed')",
                        name="ck_deployments_outcome"),
        CheckConstraint("(outcome = 'started') = (finished_at IS NULL)",
                        name="ck_deployments_phase_finished"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_deployments_times"),
        CheckConstraint("length(detail) <= 2000", name="ck_deployments_detail"),
        CheckConstraint("ci_run IS NULL OR length(ci_run) BETWEEN 1 AND 200",
                        name="ck_deployments_ci_run"),
        Index("uq_deployments_attempt_started", "attempt_id", unique=True,
              postgresql_where=text("outcome = 'started'"), sqlite_where=text("outcome = 'started'")),
        Index("uq_deployments_attempt_finished", "attempt_id", unique=True,
              postgresql_where=text("outcome <> 'started'"),
              sqlite_where=text("outcome <> 'started'")),
    )
