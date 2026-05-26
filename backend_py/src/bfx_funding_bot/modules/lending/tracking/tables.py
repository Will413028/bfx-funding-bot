from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


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
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candle_range_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candle_range_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "source", "symbol", "period_agg", "horizon_h", "spread_bucket_bps"
        ),
    )
