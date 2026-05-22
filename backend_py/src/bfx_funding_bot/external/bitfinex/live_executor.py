"""BitfinexLiveExecutor — REST submit + cancel (Phase 4.4a).

Pure fns (build_offer_payload, parse_offer_response) + I/O shell (Task 14).

Per spec §6.3:
  - submit returns status="submitted" (NOT "filled") — WS fcn fills later
  - submit failure → status="failed", venue_offer_id=None
  - cancel publishes CancelRequested event (no in-memory _pending_cancels dict)
"""
from __future__ import annotations

from typing import Any

from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.protocols import SubmittedOrder


def build_offer_payload(
    *,
    symbol: str,
    amount_usdt: float,
    rate: float,
    period_days: int,
    cid: int,
) -> dict[str, Any]:
    """Build Bitfinex POST /v2/auth/w/funding/offer/submit body.

    Per https://docs.bitfinex.com/reference/rest-auth-submit-funding-offer
    """
    return {
        "type": "LIMIT",
        "symbol": symbol,
        "amount": str(amount_usdt),
        "rate": str(rate),
        "period": period_days,
        "flags": 0,
    }


def parse_offer_response(raw: Any) -> SubmittedOrder:
    """Parse Bitfinex /funding/offer/submit response → SubmittedOrder.

    Response shape per docs:
      [MTS, TYPE, MESSAGE_ID, _, OFFER_ARRAY, CODE, STATUS, _, TEXT]
    OFFER_ARRAY[0] = OFFER_ID (used as venue_offer_id).
    STATUS = "SUCCESS" / "ERROR" / "FAILURE".

    Caller fills cid (this fn is pure parse).
    """
    if not isinstance(raw, list) or len(raw) < 7:
        raise InvariantViolation(
            f"unexpected Bitfinex offer response shape: type={type(raw).__name__}"
        )

    status_field = raw[6]
    if status_field == "SUCCESS":
        offer = raw[4]
        if not isinstance(offer, list) or len(offer) < 1:
            raise InvariantViolation(
                "Bitfinex SUCCESS response missing OFFER_ARRAY"
            )
        venue_offer_id = str(offer[0])
        return SubmittedOrder(
            cid=0,
            venue_offer_id=venue_offer_id,
            status="submitted",
            raw_response={"raw": raw},
        )

    # ERROR / FAILURE
    return SubmittedOrder(
        cid=0,
        venue_offer_id=None,
        status="failed",
        raw_response={"raw": raw, "error_text": raw[8] if len(raw) > 8 else None},
    )
