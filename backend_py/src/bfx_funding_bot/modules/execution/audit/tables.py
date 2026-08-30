"""SQLAlchemy table for immutable pre-trade execution decisions."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Index, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


class ExecutionDecisionRow(Base):
    """Append-only audit row. No update/delete repository API is exposed."""

    __tablename__ = "execution_decisions"

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    reconcile_id: Mapped[str] = mapped_column(Text, nullable=False)
    cell_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    signal_correlation_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_dependency: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    applied_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_captured_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snapshot_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_age_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    safety_result: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    execution_policy: Mapped[str] = mapped_column(Text, nullable=False)
    service_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_execution_decisions_env_occurred", "deployment_environment", "occurred_at_ms"),
        Index("idx_execution_decisions_symbol_occurred", "symbol", "occurred_at_ms"),
        Index("idx_execution_decisions_outcome_reason", "outcome", "reason_code"),
    )
