"""Authenticated Bitfinex REST reads (Phase 4.4c / 3a-recovery).

Separate from the public BitfinexREST (api-pub.bitfinex.com): authenticated
endpoints require api.bitfinex.com + HMAC-SHA384 signing (auth_ws.sign_request)
+ per-account credentials. Mirrors BitfinexLiveExecutor's auth pattern but for
read-side recovery queries.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError

# Funding-offer array indices (Bitfinex docs). No cid field exists.
_MIN_ROW_LEN = 16


@dataclass(frozen=True, slots=True)
class ActiveFundingOffer:
    venue_offer_id: str
    symbol: str
    amount: Decimal     # absolute size (offers are negative-signed at venue)
    rate: float  # display/audit-only; never used in financial arithmetic (float precision acceptable)
    period_days: int
    mts_created: int
    status: str


def parse_active_funding_offers(raw: Any) -> list[ActiveFundingOffer]:
    """Parse Bitfinex auth funding-offers response -> list[ActiveFundingOffer]."""
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding offers, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingOffer] = []
    for o in raw:
        if not isinstance(o, list) or len(o) < _MIN_ROW_LEN:
            raise BitfinexShapeError(f"funding offer row malformed: {o!r}")
        out.append(ActiveFundingOffer(
            venue_offer_id=str(o[0]),
            symbol=str(o[1]),
            amount=abs(Decimal(str(o[4]))),
            rate=float(o[14]),
            period_days=int(o[15]),
            mts_created=int(o[2]),
            status=str(o[10]),
        ))
    return out
