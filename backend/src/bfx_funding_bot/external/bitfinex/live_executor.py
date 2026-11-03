"""BitfinexLiveExecutor — REST submit + cancel (Phase 4.4a).

Pure fns (build_offer_payload, parse_offer_response) + I/O shell (Task 14).

Per spec §6.3:
  - submit returns status="submitted" (NOT "filled") — WS foc EXECUTED fills later
  - explicit venue rejection → typed SubmitRejected; transport ambiguity → typed UNKNOWN
  - cancel returns a typed CancelResult; the account command gate publishes
    CancelRequested / CancelAcknowledged around it
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal

import httpx

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.external.bitfinex.auth_rest import log_auth_http_error
from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.credentials import HasCredentials
from bfx_funding_bot.external.bitfinex.funding_rules import RULE, validate_amount
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.order_port import (
    CancelOutcome,
    CancelResult,
    FundingCancelAllResult,
    OfferRequest,
    VenueResponseShapeError,
)
from bfx_funding_bot.external.bitfinex.retry import (
    classify_httpx_exception,
    classify_httpx_response,
    transient_retry,
)
from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitCancelledNotSent,
    SubmitNotSent,
    SubmitOutcome,
    SubmitOutcomeUnknown,
    classify_submit_response,
    response_digest,
    venue_error,
)

log = logging.getLogger(__name__)

BITFINEX_REST_BASE = "https://api.bitfinex.com"
_OFFER_SUBMIT_PATH = "v2/auth/w/funding/offer/submit"
_OFFER_CANCEL_PATH = "v2/auth/w/funding/offer/cancel"
# Cancels every funding offer in one currency; idempotent, 90 req/min.
_OFFER_CANCEL_ALL_PATH = "v2/auth/w/funding/offer/cancel/all"
_CURRENCY = re.compile(r"^[A-Z0-9]{2,15}$")


def format_venue_decimal(x: Decimal) -> str:
    """Serialize an exact Decimal rate as a fixed-point string for Bitfinex.

    NEVER send exponent notation (str(Decimal("5.531E-7")) == "5.531E-7"):
    Bitfinex's funding API rejects it (HTTP 500), so the "f" format spec forces
    fixed point. The value is taken as-is -- no float ever touches a venue value.
    """
    if not isinstance(x, Decimal) or not x.is_finite():
        raise ValueError("venue decimal must be a finite Decimal")
    return f"{x:f}"


def format_offer_amount(amount: Decimal) -> str:
    """The exact funding amount on the wire: eight fixed decimals.

    The last four decimals are the submit's D3a fingerprint, its only identity
    at the venue, so an amount with more precision than the venue keeps is
    refused rather than rounded into a different fingerprint.
    """
    if (not isinstance(amount, Decimal) or not amount.is_finite()
            or amount != amount.quantize(RULE.amount_quantum)):
        raise ValueError("offer amount must be a finite Decimal with at most 8 decimals")
    return f"{amount.quantize(RULE.amount_quantum):f}"


def build_offer_payload(
    *,
    symbol: str,
    amount_usdt: Decimal,
    rate: Decimal,
    period_days: int,
) -> dict[str, Any]:
    """Build Bitfinex POST /v2/auth/w/funding/offer/submit body.

    Per https://docs.bitfinex.com/reference/rest-auth-submit-funding-offer —
    the funding-offer submit API accepts only type/symbol/amount/rate/period/flags.
    There is NO cid field (unlike trading-order submit), so no client-side dedup:
    the submit's identity is its durable attempt and amount fingerprint.
    """
    return {
        "type": "LIMIT",
        "symbol": symbol,
        "amount": format_offer_amount(amount_usdt),
        "rate": format_venue_decimal(rate),
        "period": period_days,
        "flags": 0,
    }


def parse_offer_response(raw: Any) -> SubmitOutcome:
    """Parse a Bitfinex /funding/offer/submit response into its typed outcome.

    Response shape per docs:
      [MTS, TYPE, MESSAGE_ID, _, OFFER_ARRAY, CODE, STATUS, TEXT]
    OFFER_ARRAY[0] = OFFER_ID (used as venue_offer_id).
    STATUS = "SUCCESS" / "ERROR" / "FAILURE".

    Malformed or unrecognized payloads are UNKNOWN so the submit boundary can
    persist the ambiguity.
    """
    if not isinstance(raw, list) or len(raw) < 7:
        return SubmitOutcomeUnknown(
            reason="malformed_response",
            transport_started=True,
            raw_response_digest=response_digest(raw),
        )
    # The pure parser shares the same classifier as the HTTP shell.  This
    # prevents an unrecognized status (or a SUCCESS without an ID) from
    # being mislabelled as a rejection by a direct caller.
    return classify_submit_response(200, raw, True)


def classify_cancel_response(
    raw: Any,
) -> tuple[Literal["success", "already_terminal", "other"], str | None]:
    """Classify Bitfinex cancel REST response → (rest_status, text).

    Response shape per docs ("Cancel Funding Offer"):
      [MTS, TYPE, MESSAGE_ID, _, OFFER_ARRAY, CODE, STATUS, TEXT]  (TEXT is [7])

    rest_status:
      - "success": real cancel (raw[6] == "SUCCESS")
      - "already_terminal": offer was already filled/cancelled — caller treats
        as success (Stripe-style state-convergence semantics). Detected by
        ERROR status with text containing "not found" or "not active".
      - "other": any other ERROR / FAILURE — caller logs warn + does not raise.

    Raises VenueResponseShapeError if response shape is malformed.
    """
    if not isinstance(raw, list) or len(raw) < 7:
        raise VenueResponseShapeError(
            f"unexpected Bitfinex cancel response shape: type={type(raw).__name__}",
        )
    status_field = raw[6]
    text = raw[7] if len(raw) > 7 else None
    if status_field == "SUCCESS":
        return "success", text
    if status_field == "ERROR" and isinstance(text, str):
        text_lower = text.lower()
        if "not found" in text_lower or "not active" in text_lower:
            return "already_terminal", text
    return "other", text


def classify_cancel_all_response(raw: Any) -> FundingCancelAllResult:
    """Classify ``[MTS, "foc_all-req", null, null, null, null, STATUS, TEXT]``.

    Only ``SUCCESS`` is an acknowledgement; any other status is the venue
    refusing, recorded with its own words. A shape that is not a notification
    raises, so the caller records the attempt as failed rather than guessing.
    """
    if not isinstance(raw, list) or len(raw) < 7:
        raise VenueResponseShapeError(
            f"unexpected Bitfinex cancel-all response shape: type={type(raw).__name__}",
        )
    status = raw[6] if isinstance(raw[6], str) else None
    text = raw[7][:256] if len(raw) > 7 and isinstance(raw[7], str) else None
    if status == "SUCCESS":
        return FundingCancelAllResult(outcome="acknowledged", venue_status=status, text=text)
    return FundingCancelAllResult(outcome="rejected", venue_status=status, text=text)


class FundingCancelAllClient:
    """``POST /v2/auth/w/funding/offer/cancel/all`` -- the kill path's one venue write.

    Standalone so the kill path does not depend on the rest of the executor.
    """

    def __init__(self, *, http: httpx.AsyncClient, auth_gate: AuthRequestGate,
                 base_url: str = BITFINEX_REST_BASE) -> None:
        self._http = http
        self._auth_gate = auth_gate
        self._base_url = base_url.rstrip("/")

    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: HasCredentials,
    ) -> FundingCancelAllResult:
        """Cancel every funding offer in ``currency`` (e.g. "UST"), managed or not.

        Idempotent at the venue, so transient failures are retried like a
        single cancel. Auth and other non-retryable failures raise typed
        executor errors for the caller to record; nothing here publishes a
        domain event -- the next reconcile observes what was cancelled.
        """
        if not _CURRENCY.match(currency):
            raise ValueError(f"invalid funding currency {currency!r}")
        raw = await transient_retry(self._call)(currency, ctx)
        return classify_cancel_all_response(raw)

    async def _call(self, currency: str, ctx: HasCredentials) -> Any:
        body_bytes = json.dumps({"currency": currency}).encode("utf-8")
        # Gate held per attempt (retries re-enter), released with the response.
        async with self._auth_gate.nonce("order", label="cancel_all") as nonce:
            headers = sign_request(
                body=body_bytes, nonce=nonce,
                api_secret=ctx.credentials.api_secret,
                path=_OFFER_CANCEL_ALL_PATH,
            )
            headers["bfx-apikey"] = ctx.credentials.api_key
            headers["Content-Type"] = "application/json"
            try:
                resp = await self._http.post(
                    f"{self._base_url}/{_OFFER_CANCEL_ALL_PATH}",
                    content=body_bytes, headers=headers,
                )
            except httpx.HTTPError as exc:
                raise classify_httpx_exception(exc) from exc
        if resp.status_code >= 400:
            log_auth_http_error(resp, path=_OFFER_CANCEL_ALL_PATH, credentials=ctx.credentials)
            raise classify_httpx_response(resp)
        return resp.json()


def _response_json_or_text(response: httpx.Response) -> tuple[Any, str]:
    """Read bounded response diagnostics without letting malformed JSON escape."""
    text = response.text[:1000]
    try:
        return response.json(), text
    except (TypeError, ValueError, RecursionError):  # RecursionError: nested past the decoder's limit
        return text, text


class BitfinexLiveExecutor:
    """Bitfinex REST funding-offer writes (implements ``VenueOrderPort``).

    Pure REST — no WS, no Registry dependency, no domain events. An acknowledged
    submit returns typed ``SubmitAcknowledged``; a later fill is observed by the
    ledger (BitfinexLiveWSDispatcher turns WS foc into a hint). The account
    command gate publishes the cancel audit events around :meth:`cancel`.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        configured_symbols: frozenset[str],
        auth_gate: AuthRequestGate | None = None,
        base_url: str = BITFINEX_REST_BASE,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._http = http
        self._configured_symbols = configured_symbols
        # The daemon passes its one per-key gate; see nonce.AuthRequestGate.
        self._auth_gate = auth_gate or AuthRequestGate()
        self._base_url = base_url.rstrip("/")
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def submit(
        self, offer: OfferRequest, account: HasCredentials, *,
        pre_send: Callable[[], bool] | None = None,
    ) -> SubmitOutcome:
        """Submit one funding offer, at most once.

        NOT_SENT only when no byte can have reached the venue: the symbol is not
        configured, the wait for the nonce gate was cancelled (raised as
        ``SubmitCancelledNotSent``), the request could not be built or signed,
        the funding rule refuses the amount, or ``pre_send()`` is false. Once the
        request has started every ending is classified, never NOT_SENT.
        """
        credentials = account.credentials
        if offer.symbol not in self._configured_symbols:
            return SubmitNotSent("symbol_not_configured")

        # Hold the per-key gate from taking the nonce until the response (or
        # failure) so this request reaches the venue in nonce order. Acquired
        # before the final predicates below, because waiting for it is an
        # await and none may separate those checks from starting the request.
        # As an order it waits only for the in-flight call and earlier orders,
        # never for queued reads.
        try:
            nonce = await self._auth_gate.acquire("order", label="submit")
        except asyncio.CancelledError as exc:
            # Nothing was signed or sent: say so, and keep cancelling.
            raise SubmitCancelledNotSent("cancelled_waiting_for_venue_gate") from exc
        try:
            try:
                payload = build_offer_payload(
                    symbol=offer.symbol,
                    amount_usdt=offer.amount,
                    rate=offer.rate,
                    period_days=offer.period_days,
                )
                body_bytes = json.dumps(payload).encode("utf-8")
                headers = sign_request(
                    body=body_bytes, nonce=nonce,
                    api_secret=credentials.api_secret,
                    path=_OFFER_SUBMIT_PATH,
                )
                headers["bfx-apikey"] = credentials.api_key
                headers["Content-Type"] = "application/json"
            except Exception:
                # No request has been started, so this is a durable NOT_SENT result;
                # the command gate may safely resolve the pre-transport intent.
                return SubmitNotSent("local_validation_failed")

            try:
                validate_amount(offer.amount, offer.funding_amount_evidence,
                                symbol=offer.symbol, now_ms=self._clock())
            except (ValueError, ArithmeticError):
                return SubmitNotSent("funding_rule_or_amount_invalid")
            # Final synchronous predicates after payload/signing work. No await may
            # separate the bound amount/book checks from starting the request.
            if pre_send is not None and not pre_send():
                return SubmitNotSent("decision_book_invalid_or_expired")
            transport_started = True
            try:
                resp = await self._http.post(
                    f"{self._base_url}/{_OFFER_SUBMIT_PATH}",
                    content=body_bytes, headers=headers,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                # A failed response can contain an upstream echo of credentials or
                # other sensitive data.  Keep only its digest for correlation; the
                # typed classifier still receives the parsed body in-process.
                body = e.response.text[:1000] if e.response is not None else ""
                status = e.response.status_code if e.response is not None else None
                parsed_body: Any = None
                if e.response is not None:
                    parsed_body, _ = _response_json_or_text(e.response)
                # Bitfinex answers a business rejection with a 5xx and states the
                # reason in the body, so without these two fields a refusal and an
                # outage produce the same line -- and telling them apart afterwards
                # costs an operator adjudication. Only the shape-checked code and a
                # bounded message are taken, and the message is dropped whole if it
                # echoes either credential, which is the concern that put the rest
                # of this response behind a digest.
                venue = venue_error(parsed_body)
                venue_code = venue[0] if venue is not None else None
                venue_message = venue[1] if venue is not None else None
                if venue_message is not None and (
                    credentials.api_key in venue_message
                    or credentials.api_secret in venue_message
                ):
                    venue_message = "<redacted:credential_echo>"
                log.warning(
                    "bitfinex_submit_http_error status=%s symbol=%s rate=%s amount=%s "
                    "venue_error_code=%s venue_error_message=%s response_digest=%s",
                    status,
                    payload["symbol"],
                    payload["rate"],
                    payload["amount"],
                    venue_code,
                    venue_message,
                    response_digest(body),
                )
                return classify_submit_response(status, parsed_body, transport_started)
            except asyncio.CancelledError as e:
                return classify_submit_response(None, None, transport_started, e)
            except httpx.HTTPError as e:
                log.warning("bitfinex_submit_network_error symbol=%s err_type=%s", offer.symbol, type(e).__name__)
                return classify_submit_response(None, None, transport_started, e)
            except Exception as e:
                # A response parser/shape error after the request is sent is also
                # UNKNOWN.  Never let it fall through to the old FAILED branch.
                log.warning("bitfinex_submit_response_error symbol=%s err_type=%s", offer.symbol, type(e).__name__)
                return classify_submit_response(None, None, transport_started, e)

        finally:
            self._auth_gate.release()

        try:
            parsed_body = resp.json()
        except (TypeError, ValueError, RecursionError):  # RecursionError: nested past the decoder's limit
            return SubmitOutcomeUnknown(
                reason="malformed_response",
                transport_started=True,
                raw_response_digest=response_digest(resp.text[:1000]),
                http_status=resp.status_code,
            )
        return classify_submit_response(resp.status_code, parsed_body, transport_started)

    async def cancel(
        self, venue_offer_id: str, account: HasCredentials, *,
        before_transport: Callable[[], Awaitable[None]] | None = None,
    ) -> CancelResult:
        """Cancel an offer at Bitfinex; publishes nothing.

        ``before_transport`` runs before every attempt, outside the nonce gate;
        whatever it raises propagates. Endings:
          - 401/403 → raise ExecutorAuthError → daemon exit 78
          - 5xx / network → transient_retry 3x (1s/2s/4s); exhaust → TRANSIENT_EXHAUSTED
          - other 4xx (ExecutorFatalError from classify_httpx_response) → FATAL
          - 200 SUCCESS → SUCCESS
          - 200 ERROR "not found" / "not active" → ALREADY_TERMINAL
          - 200 ERROR other → OTHER_ERROR
          - a response that is not a notification → raise VenueResponseShapeError
        """
        wrapped = transient_retry(self._cancel_http_call)
        try:
            response_json = await wrapped(venue_offer_id, account, before_transport)
        except ExecutorAuthError:
            raise  # propagate to daemon → exit 78
        except ExecutorTransientError as e:
            log.warning(
                "bitfinex_cancel_transient_exhausted voi=%s err=%r",
                venue_offer_id, e,
            )
            return CancelResult(CancelOutcome.TRANSIENT_EXHAUSTED, repr(e))
        except ExecutorFatalError as e:
            log.warning(
                "bitfinex_cancel_fatal voi=%s err=%r",
                venue_offer_id, e,
            )
            return CancelResult(CancelOutcome.FATAL, repr(e))

        rest_status, text = classify_cancel_response(response_json)
        if rest_status == "success":
            return CancelResult(CancelOutcome.SUCCESS, text)
        if rest_status == "already_terminal":
            return CancelResult(CancelOutcome.ALREADY_TERMINAL, text)
        log.warning(
            "bitfinex_cancel_other_error voi=%s text=%s",
            venue_offer_id, text,
        )
        return CancelResult(CancelOutcome.OTHER_ERROR, text)

    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: HasCredentials,
    ) -> FundingCancelAllResult:
        """Cancel every funding offer in ``currency`` (see :class:`FundingCancelAllClient`)."""
        return await FundingCancelAllClient(
            http=self._http, auth_gate=self._auth_gate, base_url=self._base_url,
        ).cancel_all_funding_offers(currency=currency, ctx=ctx)

    async def _cancel_http_call(
        self,
        venue_offer_id: str,
        account: HasCredentials,
        before_transport: Callable[[], Awaitable[None]] | None,
    ) -> Any:
        """Pure I/O — HTTP POST + classify response into typed exception.

        Returns Bitfinex response JSON on 2xx; raises ExecutorAuthError /
        ExecutorFatalError / ExecutorTransientError on 4xx/5xx/network.
        """
        if before_transport is not None:
            await before_transport()
        try:
            voi_int = int(venue_offer_id)
        except ValueError as e:
            raise ExecutorFatalError(
                f"venue_offer_id not numeric: {venue_offer_id!r}",
            ) from e
        body = {"id": voi_int}
        body_bytes = json.dumps(body).encode("utf-8")
        credentials = account.credentials
        # Gate held per attempt (transient_retry re-enters), released with the
        # response so the retry backoff never blocks other signed calls.
        async with self._auth_gate.nonce("order", label="cancel") as nonce:
            headers = sign_request(
                body=body_bytes, nonce=nonce,
                api_secret=credentials.api_secret,
                path=_OFFER_CANCEL_PATH,
            )
            headers["bfx-apikey"] = credentials.api_key
            headers["Content-Type"] = "application/json"
            try:
                resp = await self._http.post(
                    f"{self._base_url}/{_OFFER_CANCEL_PATH}",
                    content=body_bytes, headers=headers,
                )
            except httpx.HTTPError as exc:
                raise classify_httpx_exception(exc) from exc
        if resp.status_code >= 400:
            log_auth_http_error(resp, path=_OFFER_CANCEL_PATH, credentials=credentials)
            raise classify_httpx_response(resp)
        return resp.json()
