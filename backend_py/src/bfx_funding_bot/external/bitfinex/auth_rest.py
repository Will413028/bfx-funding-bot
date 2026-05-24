"""Authenticated Bitfinex REST reads (Phase 4.4c / 3a-recovery).

Separate from the public BitfinexREST (api-pub.bitfinex.com): authenticated
endpoints require api.bitfinex.com + HMAC-SHA384 signing (auth_ws.sign_request)
+ per-account credentials. Mirrors BitfinexLiveExecutor's auth pattern but for
read-side recovery queries.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext

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


BITFINEX_AUTH_REST_BASE = "https://api.bitfinex.com"
_FUNDING_OFFERS_PATH = "v2/auth/r/funding/offers"  # /{symbol} appended; no leading slash (sign_request prepends /api/)


class BitfinexAuthREST:
    """Authenticated read client for Bitfinex funding endpoints."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        base_url: str = BITFINEX_AUTH_REST_BASE,
        nonce_provider: Callable[[], int] | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._nonce_provider = nonce_provider or (lambda: int(time.time() * 1_000_000))

    async def fetch_funding_offers_raw(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> Any:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns the raw
        decoded JSON body (list of positional arrays), before parsing.

        Separated from get_active_funding_offers so callers that need the raw
        wire payload (e.g. the live contract test capturing a fixture) still go
        through the real HMAC-signed transport.

        Raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON.
        """
        path = f"{_FUNDING_OFFERS_PATH}/{symbol}"
        body_bytes = json.dumps({}).encode("utf-8")
        nonce = self._nonce_provider()
        headers = sign_request(
            body=body_bytes, nonce=nonce,
            api_secret=ctx.credentials.api_secret, path=path,
        )
        headers["bfx-apikey"] = ctx.credentials.api_key
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                f"{self._base_url}/{path}", content=body_bytes, headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise BitfinexAPIError(status_code=0, message=f"transport error: {e}", raw=None) from e
        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error", raw=resp.text,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in funding-offers response: {e}") from e

    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> list[ActiveFundingOffer]:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns parsed
        active offers. Raises BitfinexAPIError / BitfinexShapeError."""
        raw = await self.fetch_funding_offers_raw(ctx=ctx, symbol=symbol)
        return parse_active_funding_offers(raw)
