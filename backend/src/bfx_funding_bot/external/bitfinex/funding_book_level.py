"""One Bitfinex funding book level, parsed from the REST and WS book feeds."""

from dataclasses import dataclass
from typing import Any

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError


@dataclass(frozen=True, slots=True)
class FundingBookLevel:
    """One row of GET /v2/book/f{sym}/P0 — [RATE, PERIOD, COUNT, AMOUNT].

    AMOUNT > 0 = ask（貸方掛單）、< 0 = bid（借方需求）。rate 與 ticker/candle
    close 同尺度（日利率 decimal）。"""
    rate: float
    period: int
    count: int
    amount: float

    @classmethod
    def from_bitfinex(cls, entry: list[Any]) -> "FundingBookLevel":
        if not isinstance(entry, list) or len(entry) < 4:
            raise BitfinexShapeError(f"funding book row too short: {entry!r}")
        try:
            return cls(
                rate=float(entry[0]), period=int(entry[1]),
                count=int(entry[2]), amount=float(entry[3]),
            )
        except (ArithmeticError, TypeError, ValueError) as e:
            raise BitfinexShapeError(f"funding book row has invalid values: {entry!r}") from e
