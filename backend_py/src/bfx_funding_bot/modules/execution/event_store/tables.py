from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

# JSONB on Postgres, generic JSON on sqlite (unit tests).
_JSON = JSON().with_variant(JSONB, "postgresql")

# now() on Postgres, CURRENT_TIMESTAMP on sqlite (unit tests).
# func.current_timestamp() is ANSI SQL and works on both dialects.
_NOW = func.current_timestamp()

# SQLite requires INTEGER (not BIGINT) for autoincrement PKs.
_BIG_PK = BigInteger().with_variant(Integer(), "sqlite")


class EventLogRow(Base):
    """Append-only domain-event log. SoT. No UPDATE/DELETE."""

    __tablename__ = "event_log"

    event_seq: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    cid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_event_log_acct_env_seq", "account_id", "deployment_environment", "event_seq"),
        Index("idx_event_log_cid", "cid"),
        Index("idx_event_log_voi", "venue_offer_id"),
        Index(
            "uq_event_log_dedup",
            "account_id", "deployment_environment", "event_type", "venue_offer_id", "venue_seq",
            unique=True,
        ),
    )


class OfferClaimRow(Base):
    """Snapshot: offer FSM projection, keyed by cid (PENDING-ready for Plan 3)."""

    __tablename__ = "offer_claims"

    cid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    signal_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_offer_claims_acct_env", "account_id", "deployment_environment"),
        Index("idx_offer_claims_voi", "venue_offer_id"),
    )


class PositionStateRow(Base):
    """Snapshot: ledger projection. One row per (account, env)."""

    __tablename__ = "position_state"

    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
    realized_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
    last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (PrimaryKeyConstraint("account_id", "deployment_environment"),)
