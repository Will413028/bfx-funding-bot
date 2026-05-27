"""Single source of truth for the Bitfinex funding-offer wire array layout.

The same positional array is delivered by the REST `auth/r/funding/offers`
endpoint and by the WS user channel (`fos`/`fon`/`fou`/`foc`). Parsing it in
two places with divergent index assumptions was the 2026-05-26 incident root
cause. Both REST and WS MUST build their dataclasses from this one parser.

Layout (0-indexed):
  [0]=id [1]=symbol [2]=mts_create [3]=mts_update [4]=amount(signed)
  [10]=status [14]=rate [15]=period
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError

_MIN_ROW_LEN = 16


@dataclass(frozen=True, slots=True)
class FundingOfferRow:
    venue_offer_id: str
    symbol: str
    mts_create: int
    mts_update: int
    amount: Decimal        # absolute size (venue signs offers negative)
    status: str
    rate: float | None     # display/audit only; None on at-market/placeholder rows
    period_days: int | None


def parse_funding_offer_row(o: Any) -> FundingOfferRow:
    """Parse one funding-offer positional array.

    Wrong shape (non-list / too short) raises ``BitfinexShapeError``. A malformed
    *value* in a strict correctness field (id/symbol/status/amount/mts) surfaces
    as the underlying coercion error (``TypeError``/``ValueError``/``InvalidOperation``)
    — same as the REST parser; the WS caller catches all of these. ``rate``/``period``
    are display-only and None-guarded (absent on at-market/placeholder rows).
    """
    if not isinstance(o, list) or len(o) < _MIN_ROW_LEN:
        raise BitfinexShapeError(f"funding offer row malformed: {o!r}")
    rate = o[14]
    period = o[15]
    return FundingOfferRow(
        venue_offer_id=str(o[0]),
        symbol=str(o[1]),
        mts_create=int(o[2]),
        mts_update=int(o[3]),
        amount=abs(Decimal(str(o[4]))),
        status=str(o[10]),
        rate=float(rate) if rate is not None else None,
        period_days=int(period) if period is not None else None,
    )
