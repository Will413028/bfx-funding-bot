"""External-signal research tables: perp funding rates + liquidation events.

Research-only inputs for the /strategy-research EDA funnel (hypothesis:
perp funding / liquidation cascades lead-lag margin lending rates). These
tables are NOT part of the live trading path and are never read by the bot
daemon. Populated by scripts/ingest_perp_funding.py and
scripts/ingest_liquidations.py (resume-aware, idempotent upserts).

Sources (empirically probed 2026-07-19):
- Bitfinex GET /v2/status/deriv/{key}/hist — 1-min snapshots since ~2019-08.
- Bitfinex GET /v2/liquidations/hist — events since ~2019-09 (heavy rate limit).
- Binance GET /fapi/v1/fundingRate — 8h realized funding since 2019-09.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class PerpFundingRateRow(Base):
    """One perp-funding observation.

    venue="bitfinex": 1-min deriv-status snapshot; funding_rate is
    CURRENT_FUNDING (rate applied for the running 8h period),
    next_funding_accrued is the predictive accrual for the next event.
    venue="binance-usdm": one realized 8h funding settlement; mts is
    fundingTime, predictive/OI fields are NULL.
    """

    __tablename__ = "perp_funding_rates"

    venue: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_funding_accrued: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_funding_evt_mts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deriv_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("venue", "symbol", "mts"),
        Index("idx_perp_funding_symbol_mts", "symbol", "mts"),
    )


class LiquidationRow(Base):
    """One Bitfinex liquidation event (all symbols mixed in the source feed).

    A position typically emits two events sharing pos_id: the initial
    (is_match=0, price_acquired NULL) and the matched update (is_match=1).
    amount sign: positive = long position liquidated, negative = short.
    """

    __tablename__ = "liquidations"

    venue: Mapped[str] = mapped_column(Text, nullable=False)
    pos_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    base_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_match: Mapped[int] = mapped_column(Integer, nullable=False)
    is_market_sold: Mapped[int] = mapped_column(Integer, nullable=False)
    price_acquired: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("venue", "pos_id", "mts", "is_match", "is_market_sold"),
        Index("idx_liquidations_symbol_mts", "symbol", "mts"),
    )
