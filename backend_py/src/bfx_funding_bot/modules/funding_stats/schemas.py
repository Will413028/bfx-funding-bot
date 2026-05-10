"""Pydantic schema for Bitfinex /v2/funding/stats rows.

Real-Bitfinex array shape (verified by curl 2026-05-10 + cross-check with
official `bitfinex-api-py` SDK's `FundingStatistic` serializer):
[MTS, _, _, FRR, AVG_PERIOD, _, _,
 FUNDING_AMOUNT, FUNDING_AMOUNT_USED, _, _,
 FUNDING_BELOW_THRESHOLD]   (12 entries)

Note: Bitfinex's docs page lists field names but does not specify array
indices. Positions above are empirically verified — DO NOT trust the docs'
listing order as positions.

Sample raw row (fUSD, 2026-05-10):
[1778411100000, null, null, 1.12e-06, 94.98, null, null,
 5342703101.10, 5295166008.38, null, null, 203888740.29]
"""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_decimal(value: float | int | str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class FundingStat(BaseModel):
    """Funding-stats row from Bitfinex /v2/funding/stats endpoint.

    Mirrors the funding_stats table PK (symbol, mts).
    Numeric fields use Decimal to avoid float drift.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    symbol: str = Field(min_length=1)
    mts: int  # millisecond timestamp
    frr: Decimal | None = None
    avg_period: Decimal | None = None
    funding_amount: Decimal | None = None
    funding_amount_used: Decimal | None = None
    funding_below_threshold: Decimal | None = None

    @field_validator(
        "frr", "avg_period", "funding_amount",
        "funding_amount_used", "funding_below_threshold",
        mode="before",
    )
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)

    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.mts / 1000, tz=UTC)

    @classmethod
    def from_bitfinex(cls, raw: list[Any], *, symbol: str) -> "FundingStat":
        """Parse Bitfinex array shape — see module docstring for layout.

        Required min length 12 (raw[11] = FUNDING_BELOW_THRESHOLD).
        """
        if len(raw) < 12:
            raise ValueError(
                f"Expected >=12 elements in Bitfinex funding_stats row, "
                f"got {len(raw)}: {raw!r}"
            )
        return cls(
            symbol=symbol,
            mts=int(raw[0]),
            frr=_to_decimal(raw[3]),
            avg_period=_to_decimal(raw[4]),
            funding_amount=_to_decimal(raw[7]),
            funding_amount_used=_to_decimal(raw[8]),
            funding_below_threshold=_to_decimal(raw[11]),
        )
