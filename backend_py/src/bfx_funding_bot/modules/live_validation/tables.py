from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
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


class ConfigRegimeRow(Base):
    """One row per daemon boot — execution-policy regime boundaries.

    Flag flips require a restart (config is boot-immutable), so boots ARE the
    regime boundaries. Task: attribute execution-quality metrics (fill latency,
    realized APR) to the flag state that produced them, without waiting for
    weekly windows to accumulate. Telemetry, not SoT — prunable.
    """

    __tablename__ = "config_regime"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    recorded_at_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    clamp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reprice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
