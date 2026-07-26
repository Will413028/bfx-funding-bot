from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    false,
)
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
    # Finalization (2026-07-27, ADR candle-immutability-bitemporal): a candle is
    # mutable while its period is still forming and Bitfinex keeps re-pushing it.
    # Only a FINAL candle may be fed to strategy.observe() — feeding the in-flight
    # snapshot is what let live EMA drift 4.1e-2 from replay.
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    first_seen_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finalized_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "timeframe", "period_agg", "mts"),
        Index("idx_funding_candles_mts", "mts"),
    )


class FundingCandleRevisionRow(Base):
    """Append-only evidence of writes refused against an already-final candle.

    Knowledge-time record: `observed_at_ms` is when the venue told us something
    different, which is exactly what the pre-2026-07-27 in-place upsert destroyed
    (no timestamp survived, so a 4.5-year-old distortion could only be proven from
    7 days of container logs).

    Only `close` is kept: it is the sole field that reaches a strategy decision
    (`LendDecision.rate` takes `candle.close`). Widen if OHLCV analysis ever needs it.
    """

    __tablename__ = "funding_candle_revisions"

    # sqlite only auto-assigns rowid for INTEGER PRIMARY KEY, not BIGINT — the
    # variant keeps Postgres on bigint while letting the test dialect assign ids.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    period_agg: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rejected_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index(
            "idx_funding_candle_revisions_series",
            "symbol", "timeframe", "period_agg", "mts",
        ),
    )
