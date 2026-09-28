"""Authenticated Bitfinex REST reads (Phase 4.4c / 3a-recovery).

Separate from the public BitfinexREST (api-pub.bitfinex.com): authenticated
endpoints require api.bitfinex.com + HMAC-SHA384 signing (auth_ws.sign_request)
+ per-account credentials. Mirrors BitfinexLiveExecutor's auth pattern but for
read-side recovery queries.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, TypeVar

import httpx

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.submit_wire import response_digest, venue_error
from bfx_funding_bot.modules.execution.protocols import AccountContext

_Row = TypeVar("_Row")

log = logging.getLogger(__name__)

_ERROR_BODY_LOG_MAX = 200

# Total deadline for one signed read (was a 30 s per-phase httpx timeout). It
# bounds how long an order-path request -- a kill's cancel-all -- can wait for
# the in-flight read ahead of it at the shared gate.
READ_DEADLINE_S = 10.0


def log_auth_http_error(
    resp: httpx.Response, *, path: str, credentials: Credentials,
) -> None:
    """Log why the venue refused a signed request, without leaking the key.

    Bitfinex reports a stale nonce as HTTP 500 with ``["error", CODE, "nonce:
    small"]``; without the body that is indistinguishable from an outage. The
    venue's code/message is logged when the body has that shape, otherwise a
    bounded prefix of the text. Request headers (key, signature) are never
    touched, and a body that echoes either credential is dropped whole -- only
    its digest remains for correlation.
    """
    text = resp.text
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    venue = venue_error(parsed)
    if credentials.api_key in text or credentials.api_secret in text:
        venue_code: int | None = venue[0] if venue is not None else None
        body = "<redacted:credential_echo>"
    elif venue is not None:
        venue_code, body = venue
    else:
        venue_code, body = None, text[:_ERROR_BODY_LOG_MAX]
    log.warning(
        "bitfinex_auth_http_error path=%s status=%s venue_error_code=%s "
        "body=%r response_digest=%s",
        path, resp.status_code, venue_code, body, response_digest(text[:1000]),
    )


@dataclass(frozen=True, slots=True)
class ActiveFundingOffer:
    venue_offer_id: str
    symbol: str
    amount: Decimal     # absolute size (offers are negative-signed at venue)
    rate: float  # matching converts the exact wire value through str -> Decimal
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
    rate_observed: bool = True
    rate_decimal: Decimal | None = None

    @property
    def amount_remaining(self) -> Decimal:
        """Canonical full-account name for the venue's current amount."""
        return self.amount


@dataclass(frozen=True, slots=True)
class FundingOfferHistoryCoverage:
    """Auditable fence for one fully paginated offer-history query."""

    requested_start_ms: int
    requested_end_ms: int
    oldest_mts_created: int | None
    newest_mts_created: int | None
    pages: int
    complete: bool


@dataclass(frozen=True, slots=True)
class FundingOfferHistory:
    offers: tuple[ActiveFundingOffer, ...]
    coverage: FundingOfferHistoryCoverage


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
            rate_observed=row.rate is not None,
            rate_decimal=row.rate_decimal,
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
    mts_opening: int | None = None   # [13]: the originating trade's instant


_CREDIT_MIN_ROW_LEN = 13  # period is at index 12


def parse_active_funding_credits(raw: Any) -> list[ActiveFundingCredit]:
    """Parse a Bitfinex funding-credits or funding-loans response.

    Both endpoints return the same array layout (0-indexed):
      [0]=ID [1]=SYMBOL [2]=SIDE [3]=MTS_CREATE [4]=MTS_UPDATE [5]=AMOUNT
      [6]=FLAGS [7]=STATUS [8]=RATE_TYPE [9]=_ [10]=_ [11]=RATE [12]=PERIOD
      [13]=MTS_OPENING ...

    Rate and period were once read from [9] and [10], which the venue leaves
    null; that went unnoticed because they are audit fields, and was caught on
    2026-09-23 against a live loan row whose rate sat at [11] and period at [12].
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding credits, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingCredit] = []
    for o in raw:
        if not isinstance(o, list) or len(o) < _CREDIT_MIN_ROW_LEN:
            raise BitfinexShapeError(f"funding credit row malformed: {o!r}")
        rate = o[11]
        period = o[12]
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
            mts_opening=int(o[13]) if len(o) > 13 and o[13] is not None else None,
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


@dataclass(frozen=True, slots=True)
class InterestPayment:
    """One funding interest payout from the account ledger (category 28).

    Bitfinex pays once a day per currency (~01:30Z), net of its lending fee, as a
    single "Margin Funding Payment on wallet funding" entry; ``balance`` is the
    funding wallet balance after the payout. Verified against the live account
    2026-09-27 (0.0519 UST paid on 2 x 150.77 at 0.00020504/day, ratio 0.84).
    """

    ledger_id: int
    currency: str
    wallet: str | None
    mts: int
    amount: Decimal
    balance: Decimal
    description: str


INTEREST_LEDGER_CATEGORY = 28  # "margin / swap / interest payment"
_LEDGER_MIN_ROW_LEN = 9


def parse_interest_payments(raw: Any) -> list[InterestPayment]:
    """Ledger rows: [0]=ID [1]=CURRENCY [2]=WALLET [3]=MTS [4]=_ [5]=AMOUNT
    [6]=BALANCE [7]=_ [8]=DESCRIPTION."""
    if not isinstance(raw, list):
        raise BitfinexShapeError(f"expected list of ledger rows, got {type(raw).__name__}: {raw!r}")
    out: list[InterestPayment] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < _LEDGER_MIN_ROW_LEN or row[3] is None:
            raise BitfinexShapeError(f"ledger row malformed: {row!r}")
        out.append(InterestPayment(
            ledger_id=int(row[0]), currency=str(row[1]),
            wallet=str(row[2]) if row[2] is not None else None, mts=int(row[3]),
            amount=Decimal(str(row[5])), balance=Decimal(str(row[6])),
            description=str(row[8]),
        ))
    return out


@dataclass(frozen=True, slots=True)
class FundingCreditRecord:
    """One ended credit from ``funding/credits/{symbol}/hist`` (or loan, from
    ``funding/loans/{symbol}/hist``: same layout, separate id sequence).

    For a closed credit ``mts_last_payout`` is the actual close: the venue pays
    up to the moment the borrower returns the funds or the term expires
    (verified 2026-09-27: credit 466642176 opened 15:30:46Z, repaid 15:44:48Z,
    last payout 15:44:48Z; MTS_UPDATE equalled MTS_CREATE on that row).
    """

    credit_id: int
    symbol: str
    side: int | None
    mts_create: int
    mts_update: int
    amount: Decimal       # absolute size
    status: str
    rate: Decimal         # per day
    period_days: int
    mts_opening: int
    mts_last_payout: int | None


_CREDIT_HIST_MIN_ROW_LEN = 15  # MTS_LAST_PAYOUT is at index 14


def parse_funding_credit_history(raw: Any) -> list[FundingCreditRecord]:
    """Credit rows: [0]ID [1]SYMBOL [2]SIDE [3]MTS_CREATE [4]MTS_UPDATE [5]AMOUNT
    [6]FLAGS [7]STATUS [8]RATE_TYPE [9-10]_ [11]RATE [12]PERIOD [13]MTS_OPENING
    [14]MTS_LAST_PAYOUT ..."""
    if not isinstance(raw, list):
        raise BitfinexShapeError(f"expected list of credit rows, got {type(raw).__name__}: {raw!r}")
    out: list[FundingCreditRecord] = []
    for row in raw:
        if (not isinstance(row, list) or len(row) < _CREDIT_HIST_MIN_ROW_LEN
                or any(row[i] is None for i in (0, 1, 3, 4, 5, 11, 12))):
            raise BitfinexShapeError(f"credit history row malformed: {row!r}")
        out.append(FundingCreditRecord(
            credit_id=int(row[0]), symbol=str(row[1]),
            side=int(row[2]) if row[2] is not None else None,
            mts_create=int(row[3]), mts_update=int(row[4]),
            amount=abs(Decimal(str(row[5]))), status=str(row[7]),
            rate=Decimal(str(row[11])), period_days=int(row[12]),
            mts_opening=int(row[13]) if row[13] is not None else int(row[3]),
            mts_last_payout=int(row[14]) if row[14] is not None else None,
        ))
    return out


@dataclass(frozen=True, slots=True)
class FundingTrade:
    """One funding trade (an offer of ours matched): ``funding/trades/{symbol}/hist``.

    ``offer_id`` is the venue id of our submitted offer, the link from a credit
    back to the decision (and cell) that placed it.
    """

    trade_id: int
    symbol: str
    mts_create: int
    offer_id: int
    amount: Decimal       # absolute size
    rate: Decimal
    period_days: int
    maker: bool | None


_TRADE_MIN_ROW_LEN = 7  # PERIOD at index 6


def parse_funding_trades(raw: Any) -> list[FundingTrade]:
    """Trade rows: [0]ID [1]SYMBOL [2]MTS_CREATE [3]OFFER_ID [4]AMOUNT [5]RATE
    [6]PERIOD [7]MAKER."""
    if not isinstance(raw, list):
        raise BitfinexShapeError(f"expected list of funding trades, got {type(raw).__name__}: {raw!r}")
    out: list[FundingTrade] = []
    for row in raw:
        if (not isinstance(row, list) or len(row) < _TRADE_MIN_ROW_LEN
                or any(row[i] is None for i in range(_TRADE_MIN_ROW_LEN))):
            raise BitfinexShapeError(f"funding trade row malformed: {row!r}")
        maker = row[7] if len(row) > 7 else None
        out.append(FundingTrade(
            trade_id=int(row[0]), symbol=str(row[1]), mts_create=int(row[2]),
            offer_id=int(row[3]), amount=abs(Decimal(str(row[4]))),
            rate=Decimal(str(row[5])), period_days=int(row[6]),
            maker=bool(maker) if maker is not None else None,
        ))
    return out


BITFINEX_AUTH_REST_BASE = "https://api.bitfinex.com"
_FUNDING_OFFERS_PATH = "v2/auth/r/funding/offers"  # /{symbol} appended; no leading slash (sign_request prepends /api/)
_FUNDING_CREDITS_PATH = "v2/auth/r/funding/credits"
# Lent funds that no borrower has drawn into a position yet. Bitfinex lists
# them here and NOT under credits, and moves them across once they are used.
_FUNDING_LOANS_PATH = "v2/auth/r/funding/loans"
# Loan and credit ids are separate venue sequences; keep them from ever being
# mistaken for one another by an id-keyed projection.
LOAN_ID_PREFIX = "loan:"
_LEDGERS_PATH = "v2/auth/r/ledgers"  # /{currency}/hist appended
_FUNDING_TRADES_PATH = "v2/auth/r/funding/trades"  # /{symbol}/hist appended
_WALLETS_PATH = "v2/auth/r/wallets"  # no /{symbol}; sign_request prepends /api/
_PERMISSIONS_PATH = "v2/auth/r/permissions"  # no body args; sign_request prepends /api/


class BitfinexAuthREST:
    """Authenticated read client for Bitfinex funding endpoints."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        base_url: str = BITFINEX_AUTH_REST_BASE,
        auth_gate: AuthRequestGate | None = None,
        read_deadline_s: float = READ_DEADLINE_S,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._read_deadline_s = read_deadline_s
        # Share the daemon's one gate for the key; a private gate only orders
        # this client's own requests.
        self._auth_gate = auth_gate or AuthRequestGate()

    async def _signed_post(
        self, *, ctx: AccountContext, path: str, body_bytes: bytes,
    ) -> httpx.Response:
        """The only way this client talks to the venue: sign + POST under the
        shared gate, so the request arrives in nonce order. Raises
        BitfinexAPIError on transport error or deadline (status 0) or HTTP >= 400.

        Every call here is a read, so it waits behind any queued order-path
        request and is cut at a total deadline: an order (a kill) waiting for
        the gate waits for at most this long.
        """
        async with self._auth_gate.nonce("read", label=path) as nonce:
            headers = sign_request(
                body=body_bytes, nonce=nonce,
                api_secret=ctx.credentials.api_secret, path=path,
            )
            headers["bfx-apikey"] = ctx.credentials.api_key
            headers["Content-Type"] = "application/json"
            deadline = self._read_deadline_s
            try:
                async with asyncio.timeout(deadline):
                    resp = await self._http.post(
                        f"{self._base_url}/{path}", content=body_bytes, headers=headers,
                        timeout=deadline,
                    )
            except httpx.HTTPError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {e}", raw=None,
                ) from e
            except TimeoutError as e:
                raise BitfinexAPIError(
                    status_code=0, message=f"transport error: {deadline}s deadline exceeded",
                    raw=None,
                ) from e
        if resp.status_code >= 400:
            log_auth_http_error(resp, path=path, credentials=ctx.credentials)
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error", raw=resp.text,
            )
        return resp

    async def _fetch_funding_offers_response(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> httpx.Response:
        path = _FUNDING_OFFERS_PATH if symbol is None else f"{_FUNDING_OFFERS_PATH}/{symbol}"
        return await self._signed_post(ctx=ctx, path=path, body_bytes=b"{}")

    async def fetch_funding_offers_raw(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> Any:
        """Return the legacy default-decoded positional-array payload.

        This compatibility path intentionally retains JSON's float decoding.
        Typed reconciliation reads use an exact Decimal decoder instead.
        """
        resp = await self._fetch_funding_offers_response(ctx=ctx, symbol=symbol)
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in funding-offers response: {e}") from e

    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingOffer]:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns parsed
        active offers. Raises BitfinexAPIError / BitfinexShapeError."""
        resp = await self._fetch_funding_offers_response(ctx=ctx, symbol=symbol)
        try:
            raw = json.loads(resp.content, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BitfinexShapeError(
                f"invalid JSON in funding-offers response: {exc}"
            ) from exc
        return parse_active_funding_offers(raw)

    async def get_funding_offer_history(
        self,
        *,
        ctx: AccountContext,
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
        limit: int = 500,
        max_pages: int = 20,
    ) -> FundingOfferHistory:
        """Fetch the bounded funding-offer history without overstating coverage.

        Bitfinex exposes no cursor for this endpoint.  We page backwards by the
        oldest returned creation timestamp and only call the result complete
        when a short/empty page proves exhaustion or a page reaches the
        requested lower fence.
        """
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("invalid funding-offer history window")
        if limit < 1 or limit > 500:
            raise ValueError("funding-offer history limit must be between 1 and 500")
        if max_pages < 1:
            raise ValueError("funding-offer history max_pages must be positive")

        path = (
            f"{_FUNDING_OFFERS_PATH}/hist"
            if symbol is None
            else f"{_FUNDING_OFFERS_PATH}/{symbol}/hist"
        )
        cursor_end = end_ms
        pages = 0
        complete = False
        by_id: dict[str, ActiveFundingOffer] = {}
        prior_oldest: int | None = None
        for _ in range(max_pages):
            body_bytes = json.dumps(
                {"start": start_ms, "end": cursor_end, "limit": limit},
                separators=(",", ":"),
            ).encode("utf-8")
            # One gate hold per page: a trading-path call can interleave.
            resp = await self._signed_post(ctx=ctx, path=path, body_bytes=body_bytes)
            try:
                raw = json.loads(resp.content, parse_float=Decimal)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BitfinexShapeError(
                    f"invalid JSON in funding-offer-history response: {exc}"
                ) from exc
            page = parse_active_funding_offers(raw)
            pages += 1
            if not page:
                complete = True
                break
            for offer in page:
                by_id.setdefault(offer.venue_offer_id, offer)
            oldest = min(offer.mts_created for offer in page)
            if len(page) < limit:
                complete = True
                break
            if oldest <= start_ms:
                # A full boundary page may have truncated more rows sharing the
                # same timestamp; without a cursor that cannot be proven complete.
                break
            if prior_oldest is not None and oldest >= prior_oldest:
                break
            prior_oldest = oldest
            cursor_end = oldest - 1

        offers = tuple(sorted(by_id.values(), key=lambda offer: offer.mts_created))
        timestamps = [offer.mts_created for offer in offers]
        if timestamps and (
            min(timestamps) < start_ms or max(timestamps) > end_ms
        ):
            complete = False
        return FundingOfferHistory(
            offers=offers,
            coverage=FundingOfferHistoryCoverage(
                requested_start_ms=start_ms,
                requested_end_ms=end_ms,
                oldest_mts_created=min(timestamps) if timestamps else None,
                newest_mts_created=max(timestamps) if timestamps else None,
                pages=pages,
                complete=complete,
            ),
        )

    async def get_active_funding_credits(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingCredit]:
        """POST /v2/auth/r/funding/credits/{symbol} (signed). Returns parsed
        active credits. Raises BitfinexAPIError / BitfinexShapeError."""
        path = _FUNDING_CREDITS_PATH if symbol is None else f"{_FUNDING_CREDITS_PATH}/{symbol}"
        return await self._post_funding_lent(ctx=ctx, path=path)

    async def get_active_funding_loans(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingCredit]:
        """POST /v2/auth/r/funding/loans/{symbol} (signed).

        Money that has been lent is under credits only once a borrower uses it
        in a position; until then Bitfinex lists it here. Reading credits alone
        makes every freshly filled offer vanish from the books -- the wallet's
        available balance drops and nothing explains where it went. Ids carry
        LOAN_ID_PREFIX so they can never collide with a credit id.
        """
        path = _FUNDING_LOANS_PATH if symbol is None else f"{_FUNDING_LOANS_PATH}/{symbol}"
        return [
            replace(loan, credit_id=LOAN_ID_PREFIX + loan.credit_id)
            for loan in await self._post_funding_lent(ctx=ctx, path=path)
        ]

    async def get_interest_payments(
        self, *, ctx: AccountContext, currency: str, start_ms: int, end_ms: int,
        limit: int = 500, max_pages: int = 20,
    ) -> list[InterestPayment]:
        """Interest payouts in [start_ms, end_ms], oldest first.

        Payouts are daily, so a page of 500 covers over a year.
        """
        payments = await self._page_hist(
            ctx=ctx, path=f"{_LEDGERS_PATH}/{currency}/hist",
            extra={"category": INTEREST_LEDGER_CATEGORY}, parse=parse_interest_payments,
            mts=lambda entry: entry.mts, key=lambda entry: entry.ledger_id,
            start_ms=start_ms, end_ms=end_ms, limit=limit, max_pages=max_pages,
        )
        return sorted(payments, key=lambda entry: (entry.mts, entry.ledger_id))

    async def get_funding_credit_history(
        self, *, ctx: AccountContext, symbol: str, start_ms: int, end_ms: int,
        kind: Literal["credit", "loan"] = "credit", limit: int = 500, max_pages: int = 20,
    ) -> list[FundingCreditRecord]:
        """Ended credits (or, with ``kind="loan"``, loans) updated in
        [start_ms, end_ms], oldest update first. Both share one row layout.

        Paged by MTS_UPDATE. Which timestamp the venue filters on is not
        documented; rows are deduplicated by id, so a re-read is harmless.
        """
        base = _FUNDING_CREDITS_PATH if kind == "credit" else _FUNDING_LOANS_PATH
        credits = await self._page_hist(
            ctx=ctx, path=f"{base}/{symbol}/hist", extra={},
            parse=parse_funding_credit_history, mts=lambda c: c.mts_update,
            key=lambda c: c.credit_id, start_ms=start_ms, end_ms=end_ms,
            limit=limit, max_pages=max_pages,
        )
        return sorted(credits, key=lambda c: (c.mts_update, c.credit_id))

    async def get_funding_trades(
        self, *, ctx: AccountContext, symbol: str, start_ms: int, end_ms: int,
        limit: int = 500, max_pages: int = 20,
    ) -> list[FundingTrade]:
        """Funding trades (our offers matched) in [start_ms, end_ms], oldest first."""
        trades = await self._page_hist(
            ctx=ctx, path=f"{_FUNDING_TRADES_PATH}/{symbol}/hist", extra={},
            parse=parse_funding_trades, mts=lambda t: t.mts_create,
            key=lambda t: t.trade_id, start_ms=start_ms, end_ms=end_ms,
            limit=limit, max_pages=max_pages,
        )
        return sorted(trades, key=lambda t: (t.mts_create, t.trade_id))

    async def _page_hist(
        self, *, ctx: AccountContext, path: str, extra: dict[str, Any],
        parse: Callable[[Any], list[_Row]], mts: Callable[[_Row], int],
        key: Callable[[_Row], int], start_ms: int, end_ms: int, limit: int, max_pages: int,
    ) -> list[_Row]:
        """A ``/hist`` endpoint returns newest first: page backwards by the
        oldest timestamp of each page until a short page, the window start, or
        no progress."""
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError(f"invalid window for {path}")
        by_key: dict[int, _Row] = {}
        cursor_end = end_ms
        for _ in range(max_pages):
            body = {**extra, "start": start_ms, "end": cursor_end, "limit": limit}
            page = parse(await self._post_signed(ctx=ctx, path=path, body=body))
            for row in page:
                by_key.setdefault(key(row), row)
            if len(page) < limit:
                break
            oldest = min(mts(row) for row in page)
            if oldest <= start_ms or oldest >= cursor_end:
                break
            cursor_end = oldest - 1
        return list(by_key.values())

    async def _post_signed(self, *, ctx: AccountContext, path: str, body: dict[str, Any]) -> Any:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        resp = await self._signed_post(ctx=ctx, path=path, body_bytes=body_bytes)
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BitfinexShapeError(f"invalid JSON in {path} response: {e}") from e

    async def _post_funding_lent(
        self, *, ctx: AccountContext, path: str,
    ) -> list[ActiveFundingCredit]:
        resp = await self._signed_post(ctx=ctx, path=path, body_bytes=b"{}")
        try:
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in {path} response: {e}") from e
        return parse_active_funding_credits(raw)

    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal:
        """POST /v2/auth/r/wallets (signed). Returns Σ available of FUNDING
        wallets for `currency` (0 if none). Same error contract as offers/credits:
        raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON / shape."""
        path = _WALLETS_PATH
        resp = await self._signed_post(ctx=ctx, path=path, body_bytes=b"{}")
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
        resp = await self._signed_post(ctx=ctx, path=path, body_bytes=b"{}")
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
        resp = await self._signed_post(ctx=ctx, path=path, body_bytes=b"{}")
        try:
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in permissions response: {e}") from e
        return parse_key_permissions(raw)
