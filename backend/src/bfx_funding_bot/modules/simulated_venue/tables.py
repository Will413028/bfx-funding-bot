"""The simulated venue's durable event log (`sim_venue_event`), insert-only.

Independent of the bot's tables on purpose: no foreign key to `exchange_accounts`, and the
venue never reads ledger journals. The realm CHECK makes a `prod` row impossible by
construction in every database the one migration chain reaches (ADR 2026-10-03 D1). The
insert-only triggers, the revoked default privileges and the `bfx_bot` SELECT/INSERT grant
live in the migration, because SQLAlchemy metadata cannot express them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

SIM_VENUE_EVENT_TABLE = "sim_venue_event"


class SimVenueEventRow(Base):
    __tablename__ = SIM_VENUE_EVENT_TABLE

    exchange_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    # 1-based position in the scope's log; the primary key makes a second writer on the
    # same position fail instead of overwrite (optimistic concurrency).
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False)
    # Bookkeeping only; the venue's own time is in the payload (`mts`, injected clock).
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "seq", name="pk_sim_venue_event"),
        CheckConstraint(
            "deployment_environment IN ('shadow', 'ci')", name="ck_sim_venue_event_realm"),
        CheckConstraint("seq >= 1", name="ck_sim_venue_event_seq"),
        CheckConstraint("exchange_account_id <> ''", name="ck_sim_venue_event_account"),
        CheckConstraint("schema_version >= 1", name="ck_sim_venue_event_version"),
    )
