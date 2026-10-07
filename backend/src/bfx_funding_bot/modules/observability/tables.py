"""Observability persistence: ``bot_runs`` -- one row per bot process that booted as the writer.

A process inserts its row once it holds the writer lock and has booted, and records how
it ended on the way out (``modules/observability/bot_runs.py``). A row the next boot
finds without an end is an unclean exit nothing else reported (loop watchdog, OOM,
SIGKILL, segfault, host crash); that boot alerts and marks it ``unclean``. Not part of
the capital ledger.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

# How a run ended. ``unclean`` is written by the NEXT boot, never by the run itself.
END_REASONS = ("clean_stop", "fatal", "boot_refused", "unclean")


class BotRunRow(Base):
    __tablename__ = "bot_runs"

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_name: Mapped[str] = mapped_column(Text, nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    # Both set together: by the run on its way out, or by the next boot (``unclean``,
    # then ``end_recorded_at_ms`` is when that boot noticed, not when the run died).
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_recorded_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "end_reason IN ('clean_stop', 'fatal', 'boot_refused', 'unclean')",
            name="ck_bot_runs_end_reason",
        ),
        CheckConstraint(
            "(end_reason IS NULL) = (end_recorded_at_ms IS NULL)",
            name="ck_bot_runs_end_pair",
        ),
        Index("ix_bot_runs_scope_started", "exchange_account_id", "deployment_environment",
              "started_at_ms"),
    )
