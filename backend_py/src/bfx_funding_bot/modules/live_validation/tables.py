from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

_NOW = func.current_timestamp()


class AttributionWeeklyRow(Base):
    """E3 (b) — per-cell weekly fee-adjusted realized APR（operator 儀表）。

    由 scripts/run_weekly_attribution.py 每週全量重算 + upsert（event-sourced：
    event_log 是 SoT，本表是可隨時重建的 read model）。webapi 只 SELECT
    （GRANT 是部署 runbook step，不在 migration）。
    """

    __tablename__ = "attribution_weekly"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    cell: Mapped[str] = mapped_column(Text, primary_key=True)
    week_start_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    week_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    n_fills: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    net_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    capital_days: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_close_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_frr_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_frr_util_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )
