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
    # Additive normalized fields used by full-account reconciliation.  The
    # legacy parser contract above remains intact for WS consumers.
    amount_original: Decimal | None = None
    offer_type: str | None = None
    flags: dict[str, Any] | int | None = None


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
    amount = abs(Decimal(str(o[4])))
    original_raw = o[5]
    amount_original = (
        abs(Decimal(str(original_raw))) if original_raw is not None else amount
    )
    flags = o[9] if len(o) > 9 else None
    return FundingOfferRow(
        venue_offer_id=str(o[0]),
        symbol=str(o[1]),
        mts_create=int(o[2]),
        mts_update=int(o[3]),
        amount=amount,
        status=str(o[10]),
        rate=float(rate) if rate is not None else None,
        period_days=int(period) if period is not None else None,
        amount_original=amount_original,
        offer_type=str(o[6]) if o[6] is not None else None,
        flags=flags if isinstance(flags, (dict, int)) else None,
    )
