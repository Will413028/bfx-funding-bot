from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import JSON_DOCUMENT, Base


class FillRateModelArtifactRow(Base):
    """Versioned provenance for one learned (source, symbol, period, horizon) model."""

    __tablename__ = "fill_rate_model_artifacts"

    artifact_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    period_agg: Mapped[str] = mapped_column(Text, nullable=False)
    horizon_h: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    training_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    training_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cutoff_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_min_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSON_DOCUMENT,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FillRateStatsRow(Base):
    """Fill-rate learning stats, keyed by (source, symbol, period_agg, horizon_h, spread_bucket_bps)."""

    __tablename__ = "fill_rate_stats"

    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'candle' | (future) 'book'/'own_fill'
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    period_agg: Mapped[str] = mapped_column(Text, nullable=False)
    horizon_h: Mapped[int] = mapped_column(Integer, nullable=False)
    spread_bucket_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_prob: Mapped[float] = mapped_column(Float, nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    ttf_p50_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ttf_p90_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mean_ttf_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("fill_rate_model_artifacts.artifact_hash"),
        nullable=True,
    )
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candle_range_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candle_range_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "source", "symbol", "period_agg", "horizon_h", "spread_bucket_bps"
        ),
    )
