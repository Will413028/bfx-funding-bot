from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class FundingCandle(BaseModel):
    """Funding-rate candle from Bitfinex v2 candles endpoint.

    Mirrors the funding_candles table PK (symbol, timeframe, period_agg, mts).
    Numeric fields use Decimal to avoid float drift in financial math.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    period_agg: str = Field(min_length=1)
    mts: int  # millisecond timestamp
    open: Decimal | None = None
    close: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None

    @field_validator("open", "close", "high", "low", "volume", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)

    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.mts / 1000, tz=UTC)

    @classmethod
    def from_bitfinex(
        cls,
        raw: list[Any],
        *,
        symbol: str,
        timeframe: str,
        period_agg: str,
    ) -> "FundingCandle":
        """Parse Bitfinex's array-shaped candle: [mts, open, close, high, low, volume]."""
        if len(raw) != 6:
            raise ValueError(
                f"Expected 6 elements in Bitfinex candle, got {len(raw)}: {raw!r}"
            )
        mts, o, c, h, lo, v = raw
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            period_agg=period_agg,
            mts=int(mts),
            open=_to_decimal(o),
            close=_to_decimal(c),
            high=_to_decimal(h),
            low=_to_decimal(lo),
            volume=_to_decimal(v),
        )
