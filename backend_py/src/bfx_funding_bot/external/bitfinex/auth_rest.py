"""Authenticated Bitfinex REST reads (Phase 4.4c / 3a-recovery).

Separate from the public BitfinexREST (api-pub.bitfinex.com): authenticated
endpoints require api.bitfinex.com + HMAC-SHA384 signing (auth_ws.sign_request)
+ per-account credentials. Mirrors BitfinexLiveExecutor's auth pattern but for
read-side recovery queries.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
from bfx_funding_bot.modules.execution.protocols import AccountContext


@dataclass(frozen=True, slots=True)
class ActiveFundingOffer:
    venue_offer_id: str
    symbol: str
    amount: Decimal     # absolute size (offers are negative-signed at venue)
    rate: float  # display/audit-only; never used in financial arithmetic (float precision acceptable)
    period_days: int
    mts_created: int
    status: str
    # Full-account reconciliation keeps the venue's original/current amounts
    # and update metadata.  Defaults preserve the lightweight legacy model used
    # by existing callers and fixtures.
    amount_original: Decimal | None = None
    mts_updated: int | None = None
    offer_type: str | None = None
    flags: dict[str, Any] | int | None = None

    @property
    def amount_remaining(self) -> Decimal:
        """Canonical full-account name for the venue's current amount."""
        return self.amount


def parse_active_funding_offers(raw: Any) -> list[ActiveFundingOffer]:
    """Parse Bitfinex auth funding-offers response -> list[ActiveFundingOffer].

    Layout is owned by funding_offer_row.parse_funding_offer_row (shared with WS).
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding offers, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingOffer] = []
    for o in raw:
        row = parse_funding_offer_row(o)
        out.append(ActiveFundingOffer(
            venue_offer_id=row.venue_offer_id,
            symbol=row.symbol,
            amount=row.amount,
            rate=row.rate if row.rate is not None else 0.0,
            period_days=row.period_days if row.period_days is not None else 0,
            mts_created=row.mts_create,
            status=row.status,
            amount_original=row.amount_original,
            mts_updated=row.mts_update,
            offer_type=row.offer_type,
            flags=row.flags,
        ))
    return out


@dataclass(frozen=True, slots=True)
class ActiveFundingCredit:
    credit_id: str
    symbol: str
    amount: Decimal     # absolute size (positive for lender)
    rate: float  # display/audit-only; float precision acceptable
    period_days: int
    status: str
    mts_created: int | None = None
    mts_updated: int | None = None
    flags: dict[str, Any] | int | None = None


_CREDIT_MIN_ROW_LEN = 11  # period is at index 10


def parse_active_funding_credits(raw: Any) -> list[ActiveFundingCredit]:
    """Parse Bitfinex auth funding-credits response -> list[ActiveFundingCredit].

    Credits array layout (0-indexed):
      [0]=ID [1]=SYMBOL [2]=SIDE [3]=MTS_CREATE [4]=MTS_UPDATE [5]=AMOUNT
      [6]=FLAGS [7]=STATUS [8]=RATE_TYPE [9]=RATE [10]=PERIOD ...
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding credits, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingCredit] = []
    for o in raw:
        if not isinstance(o, list) or len(o) < _CREDIT_MIN_ROW_LEN:
            raise BitfinexShapeError(f"funding credit row malformed: {o!r}")
        rate = o[9]
        period = o[10]
        out.append(ActiveFundingCredit(
            credit_id=str(o[0]),
            symbol=str(o[1]),
            amount=abs(Decimal(str(o[5]))),
            status=str(o[7]),
            rate=float(rate) if rate is not None else 0.0,
            period_days=int(period) if period is not None else 0,
            mts_created=int(o[3]) if o[3] is not None else None,
            mts_updated=int(o[4]) if o[4] is not None else None,
            flags=o[6] if isinstance(o[6], (dict, int)) else None,
        ))
    return out


@dataclass(frozen=True, slots=True)
class FundingWallet:
    wallet_type: str   # "funding" / "exchange" / "margin"
    currency: str      # "UST" / "USD" / ...
    balance: Decimal
    available: Decimal  # AVAILABLE_BALANCE; deposit-wallet free portion


_WALLET_MIN_ROW_LEN = 5  # AVAILABLE_BALANCE at index 4


def parse_wallets(raw: Any) -> list[FundingWallet]:
    """Parse Bitfinex /v2/auth/r/wallets response -> list[FundingWallet].

    Layout: [0]=WALLET_TYPE [1]=CURRENCY [2]=BALANCE [3]=UNSETTLED_INTEREST
            [4]=AVAILABLE_BALANCE ... AVAILABLE_BALANCE may be null (venue has
    not computed it) -> treated as 0 (conservative: never deploy on unknown funds).
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of wallets, got {type(raw).__name__}: {raw!r}"
        )
    out: list[FundingWallet] = []
    for w in raw:
        if not isinstance(w, list) or len(w) < _WALLET_MIN_ROW_LEN:
            raise BitfinexShapeError(f"wallet row malformed: {w!r}")
        available_raw = w[4]
        out.append(FundingWallet(
            wallet_type=str(w[0]),
            currency=str(w[1]),
            balance=Decimal(str(w[2])),
            available=Decimal(str(available_raw)) if available_raw is not None else Decimal("0"),
        ))
    return out


@dataclass(frozen=True, slots=True)
class KeyPermissions:
    """scope -> (read, write). Bitfinex /v2/auth/r/permissions rows are
    [scope, read(0/1), write(0/1)]."""

    scopes: dict[str, tuple[bool, bool]]

    def can(self, scope: str, *, write: bool) -> bool:
        read_flag, write_flag = self.scopes.get(scope, (False, False))
        return write_flag if write else read_flag


def parse_key_permissions(raw: Any) -> KeyPermissions:
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of permission rows, got {type(raw).__name__}: {raw!r}"
        )
    scopes: dict[str, tuple[bool, bool]] = {}
    for row in raw:
        if not isinstance(row, list) or len(row) < 3:
            raise BitfinexShapeError(f"permission row malformed: {row!r}")
        try:
            scopes[str(row[0])] = (bool(int(row[1])), bool(int(row[2])))
        except (ValueError, TypeError) as exc:
            raise BitfinexShapeError(
                f"permission flag not int-coercible in row {row!r}"
            ) from exc
    return KeyPermissions(scopes=scopes)


BITFINEX_AUTH_REST_BASE = "https://api.bitfinex.com"
_FUNDING_OFFERS_PATH = "v2/auth/r/funding/offers"  # /{symbol} appended; no leading slash (sign_request prepends /api/)
_FUNDING_CREDITS_PATH = "v2/auth/r/funding/credits"
_WALLETS_PATH = "v2/auth/r/wallets"  # no /{symbol}; sign_request prepends /api/
_PERMISSIONS_PATH = "v2/auth/r/permissions"  # no body args; sign_request prepends /api/


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
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> Any:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns the raw
        decoded JSON body (list of positional arrays), before parsing.

        Separated from get_active_funding_offers so callers that need the raw
        wire payload (e.g. the live contract test capturing a fixture) still go
        through the real HMAC-signed transport.

        Raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON.
        """
        path = _FUNDING_OFFERS_PATH if symbol is None else f"{_FUNDING_OFFERS_PATH}/{symbol}"
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
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingOffer]:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns parsed
        active offers. Raises BitfinexAPIError / BitfinexShapeError."""
        raw = await self.fetch_funding_offers_raw(ctx=ctx, symbol=symbol)
        return parse_active_funding_offers(raw)

    async def get_active_funding_credits(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingCredit]:
        """POST /v2/auth/r/funding/credits/{symbol} (signed). Returns parsed
        active credits. Raises BitfinexAPIError / BitfinexShapeError."""
        path = _FUNDING_CREDITS_PATH if symbol is None else f"{_FUNDING_CREDITS_PATH}/{symbol}"
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
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in funding-credits response: {e}") from e
        return parse_active_funding_credits(raw)

    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal:
        """POST /v2/auth/r/wallets (signed). Returns Σ available of FUNDING
        wallets for `currency` (0 if none). Same error contract as offers/credits:
        raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON / shape."""
        path = _WALLETS_PATH
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
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in wallets response: {e}") from e
        wallets = parse_wallets(raw)
        return sum(
            (w.available for w in wallets
             if w.wallet_type == "funding" and w.currency == currency),
            Decimal("0"),
        )

    async def get_funding_available_all(
        self, *, ctx: AccountContext,
    ) -> Mapping[str, Decimal]:
        """Return available funding-wallet balances for every currency.

        The result keys use Bitfinex funding symbols (``fUST``/``fUSD``),
        matching offers and credits so the observation projector never has to
        infer a currency from strategy configuration.
        """
        path = _WALLETS_PATH
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
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in wallets response: {e}") from e
        wallets = parse_wallets(raw)
        out: dict[str, Decimal] = {}
        for wallet in wallets:
            if wallet.wallet_type != "funding":
                continue
            symbol = wallet.currency if wallet.currency.startswith("f") else f"f{wallet.currency}"
            out[symbol] = out.get(symbol, Decimal("0")) + wallet.available
        return out

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        """POST /v2/auth/r/permissions (signed). Returns the key's scope→(read,write)
        map. Same error contract as the other auth reads: BitfinexAPIError on
        transport/HTTP error (status_code=0 for transport), BitfinexShapeError on
        invalid JSON / shape."""
        path = _PERMISSIONS_PATH
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
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in permissions response: {e}") from e
        return parse_key_permissions(raw)
