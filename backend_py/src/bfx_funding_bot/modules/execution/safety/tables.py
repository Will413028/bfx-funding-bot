"""Safety-module persistence: nav_peak (guard calibration) + trading_halt (kill switch).

The two have OPPOSITE failure postures and must not be refactored into a shared
shape: losing a nav_peak only regresses the high-water mark (fail-permissive),
whereas an unreadable trading_halt must stop trading (fail-closed).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class NavPeakRow(Base):
    __tablename__ = "nav_peak"

    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    peak: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("account_id", "deployment_environment", "symbol"),
    )


class TradingHaltRow(Base):
    """Append-only halt/resume transitions. Current state = highest id for the realm.

    Append-only, not one mutable row, because the history IS the audit trail:
    "who resumed real-money trading, when, and on what grounds" is precisely the
    question that had no answer on 2026-07-27, and an overwritten row answers it
    even less. Rows are never updated or deleted.

    Ordering is by ``id``, never by ``created_at_ms``: the timestamp is supplied
    by the caller, and clock skew or a backfilled row must not be able to
    resurrect a superseded state on a safety control.
    """

    __tablename__ = "trading_halt"

    # sqlite only auto-assigns rowid for INTEGER PRIMARY KEY — a plain BigInteger
    # PK stays NULL there and every insert collides.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Who made the transition (admin endpoint, bootstrap, an operator name).
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index(
            "ix_trading_halt_realm_id",
            "account_id", "deployment_environment", "id",
        ),
    )
