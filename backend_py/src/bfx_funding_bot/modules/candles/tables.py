from sqlalchemy import BigInteger, Float, Index, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class FundingCandleRow(Base):
    __tablename__ = "funding_candles"

    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    period_agg: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "timeframe", "period_agg", "mts"),
        Index("idx_funding_candles_mts", "mts"),
    )


class FundingStatRow(Base):
    __tablename__ = "funding_stats"

    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frr: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_period: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_amount_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_below_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "mts"),
        Index("idx_funding_stats_mts", "mts"),
    )
