"""Safety-module persistence: nav_peak — per-symbol all-time NAV high-water mark.

One row per (account, env, symbol). Written by ReconcileNavTracker on every new
peak; read once at boot so drawdown_pct survives restarts (3e21657 limitation).
NOT part of the event-sourced ledger — this is guard-calibration state, and
losing it only regresses the peak to the last saved value (fail-permissive).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, Numeric, PrimaryKeyConstraint, Text
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
