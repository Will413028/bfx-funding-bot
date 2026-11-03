"""Marketfeed persistence: funding_book_snapshots — self-collected book history.

Bitfinex serves NO historical funding-book data; book-aware strategies
(spike-rung ladder, E2-class pricing) can only ever be backtested on data we
record ourselves, starting the day this table went live (2026-07-19). Rows are
append-only snapshots; NOT part of the capital ledger.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import JSON_DOCUMENT, Base

_JSON = JSON_DOCUMENT
_NOW = text("CURRENT_TIMESTAMP")
_BIG_PK = BigInteger().with_variant(Integer(), "sqlite")  # sqlite autoincrement 限 INTEGER PK


class FundingBookSnapshotRow(Base):
    __tablename__ = "funding_book_snapshots"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Derived scalars for cheap querying; full depth lives in payload.
    best_bid_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    best_ask_rate: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    bid_depth: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    ask_depth: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # {"asks": [[rate, period, count, amount], ...], "bids": [...]} — amounts
    # stored positive on both sides (side already encoded by the key).
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_book_snapshots_symbol_ts", "symbol", "captured_at_ms"),
    )
