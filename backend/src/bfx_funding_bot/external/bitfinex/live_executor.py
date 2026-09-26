"""BitfinexLiveExecutor — REST submit + cancel (Phase 4.4a).

Pure fns (build_offer_payload, parse_offer_response) + I/O shell (Task 14).

Per spec §6.3:
  - submit returns status="submitted" (NOT "filled") — WS foc EXECUTED fills later
  - explicit venue rejection → typed SubmitRejected; transport ambiguity → typed UNKNOWN
  - cancel publishes CancelRequested event (no in-memory _pending_cancels dict)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.external.bitfinex.funding_rules import RULE, validate_amount
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    FundingCancelAllResult,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.retry import (
    classify_httpx_exception,
    classify_httpx_response,
    transient_retry,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcome,
    SubmitOutcomeUnknown,
    classify_submit_response,
    response_digest,
    venue_error,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName

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
    There is NO cid field (unlike trading-order submit), so no client-side dedup;
    the internal cid lives only in our event log, never in this payload.
    """
    return {
        "type": "LIMIT",
        "symbol": symbol,
        "amount": format_offer_amount(amount_usdt),
        "rate": format_venue_decimal(rate),
        "period": period_days,
        "flags": 0,
    }


def parse_offer_response(raw: Any) -> SubmittedOrder:
    """Parse Bitfinex /funding/offer/submit response → SubmittedOrder.

    Response shape per docs:
      [MTS, TYPE, MESSAGE_ID, _, OFFER_ARRAY, CODE, STATUS, _, TEXT]
    OFFER_ARRAY[0] = OFFER_ID (used as venue_offer_id).
    STATUS = "SUCCESS" / "ERROR" / "FAILURE".

    Caller fills cid (this fn is pure parse).  Malformed/unrecognized payloads
    are returned as UNKNOWN so the submit boundary can persist ambiguity.
    """
    if not isinstance(raw, list) or len(raw) < 7:
        outcome: SubmitOutcome = SubmitOutcomeUnknown(
            reason="malformed_response",
            transport_started=True,
            raw_response_digest=response_digest(raw),
        )
    else:
        # The pure parser shares the same classifier as the HTTP shell.  This
        # prevents an unrecognized status (or a SUCCESS without an ID) from
        # being mislabelled as a rejection by a direct caller.
        outcome = classify_submit_response(200, raw, True)
    venue_offer_id = (
        outcome.venue_offer_id if isinstance(outcome, SubmitAcknowledged) else None
    )
    bounded_response = getattr(outcome, "raw_response", None)
    if bounded_response is None:
        bounded_response = {"response_digest": response_digest(raw)}
        if isinstance(raw, list) and len(raw) > 8 and isinstance(raw[8], str):
            bounded_response["error_text"] = raw[8][:256]
    return SubmittedOrder(
        cid=0,
        venue_offer_id=venue_offer_id,
        outcome=outcome,
        raw_response=bounded_response,
    )


def classify_cancel_response(
    raw: Any,
) -> tuple[Literal["success", "already_terminal", "other"], str | None]:
    """Classify Bitfinex cancel REST response → (rest_status, text).

    rest_status:
      - "success": real cancel (raw[6] == "SUCCESS")
      - "already_terminal": offer was already filled/cancelled — caller treats
        as success (Stripe-style state-convergence semantics). Detected by
        ERROR status with text containing "not found" or "not active".
      - "other": any other ERROR / FAILURE — caller logs warn + does not raise.

    Raises InvariantViolation if response shape is malformed.
    """
    if not isinstance(raw, list) or len(raw) < 7:
        raise InvariantViolation(
            f"unexpected Bitfinex cancel response shape: type={type(raw).__name__}",
        )
    status_field = raw[6]
    text = raw[8] if len(raw) > 8 else None
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
        raise InvariantViolation(
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

    def __init__(self, *, http: httpx.AsyncClient, nonce_provider: Callable[[], int],
                 base_url: str = BITFINEX_REST_BASE) -> None:
        self._http = http
        self._nonce_provider = nonce_provider
        self._base_url = base_url.rstrip("/")

    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: AccountContext,
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

    async def _call(self, currency: str, ctx: AccountContext) -> Any:
        body_bytes = json.dumps({"currency": currency}).encode("utf-8")
        headers = sign_request(
            body=body_bytes, nonce=self._nonce_provider(),
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
            raise classify_httpx_response(resp)
        return resp.json()


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


def _response_json_or_text(response: httpx.Response) -> tuple[Any, str]:
    """Read bounded response diagnostics without letting malformed JSON escape."""
    text = response.text[:1000]
    try:
        return response.json(), text
    except (TypeError, ValueError):
        return text, text


def _order_from_outcome(
    *,
    cid: int,
    reference: ReservationRef,
    outcome: SubmitOutcome,
    raw_response: Any | None = None,
) -> SubmittedOrder:
    venue_offer_id = (
        outcome.venue_offer_id
        if isinstance(outcome, SubmitAcknowledged)
        else None
    )
    bound_reference = (
        reference.bind_venue_offer(venue_offer_id)
        if venue_offer_id is not None
        else reference
    )
    return SubmittedOrder(
        cid=cid,
        venue_offer_id=venue_offer_id,
        outcome=outcome,
        raw_response=(
            raw_response
            if raw_response is not None
            else getattr(outcome, "raw_response", None)
        ),
        reservation_ref=bound_reference,
    )


class BitfinexLiveExecutor:
    """Bitfinex REST funding offer executor.

    Pure REST — no WS, no Registry dependency. An acknowledged submit returns
    typed ``SubmitAcknowledged`` (compatibility status="submitted"); WS foc
    EXECUTED (handled by BitfinexLiveWSDispatcher) publishes OrderFilled later.

    cancel publishes CancelRequested event (first-class) — replaces former
    _pending_cancels dict pattern.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        event_sink: _EventSink,
        bus: DomainEventBus,
        phase: Phase,
        strategy: StrategyName,
        configured_symbols: frozenset[str],
        cell: str,
        nonce_provider: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
        base_url: str = BITFINEX_REST_BASE,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._http = http
        self._events = event_sink
        self._bus = bus
        self._phase = phase
        self._strategy = strategy
        self._configured_symbols = configured_symbols
        self._cell = cell
        self._nonce_provider = nonce_provider or (lambda: int(time.time() * 1_000_000))
        self._date_provider = date_provider or (lambda: date.today())
        self._base_url = base_url.rstrip("/")
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        decision = ready.decision
        # cid centralized by ReservationEmittingMiddleware (A2); direct callers
        # fall back to deterministic generation (CC2 capture-once date).
        if cid is None:
            cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        reference = reservation_ref or ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=cid,
            signal_correlation_id=decision.signal_correlation_id,
        )
        if (
            reference.execution_decision_id != ready.decision_id
            or reference.cid != cid
            or reference.signal_correlation_id != decision.signal_correlation_id
            or reference.venue_offer_id is not None
        ):
            raise InvariantViolation("reservation_ref conflicts with ReadyToSubmit request")

        if not decision.symbol:
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=SubmitNotSent("symbol_missing"),
            )
        if decision.symbol not in self._configured_symbols:
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=SubmitNotSent("symbol_not_configured"),
            )
        amount = decision.offer_amount_usdt
        rate = decision.offer_rate
        period = decision.offer_duration_days
        if (
            amount is None
            or amount <= 0
            or rate is None
            or rate <= 0
            or period is None
            or period <= 0
        ):
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=SubmitNotSent("invalid_submit_payload"),
            )

        try:
            payload = build_offer_payload(
                symbol=decision.symbol,
                amount_usdt=amount,
                rate=rate,
                period_days=period,
            )
            body_bytes = json.dumps(payload).encode("utf-8")
            nonce = self._nonce_provider()
            headers = sign_request(
                body=body_bytes, nonce=nonce,
                api_secret=ctx.credentials.api_secret,
                path=_OFFER_SUBMIT_PATH,
            )
            headers["bfx-apikey"] = ctx.credentials.api_key
            headers["Content-Type"] = "application/json"
        except Exception:
            # No request has been started, so this is a durable NOT_SENT result;
            # the command gate may safely resolve the pre-transport intent.
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=SubmitNotSent("local_validation_failed"),
            )

        try:
            validate_amount(amount, ready.funding_amount_evidence,
                            symbol=decision.symbol, now_ms=self._clock())
        except (ValueError, ArithmeticError):
            return _order_from_outcome(cid=cid, reference=reference,
                outcome=SubmitNotSent("funding_rule_or_amount_invalid"))
        # Final synchronous predicates after payload/signing work. No await may
        # separate the bound amount/book checks from starting the request.
        if ctx.before_submit_transport is not None and not ctx.before_submit_transport():
            return _order_from_outcome(cid=cid, reference=reference,
                outcome=SubmitNotSent("decision_book_invalid_or_expired"))
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
                ctx.credentials.api_key in venue_message
                or ctx.credentials.api_secret in venue_message
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
            outcome = classify_submit_response(
                status,
                parsed_body,
                transport_started,
            )
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=outcome,
                raw_response={
                    "http_status": status,
                    "response_digest": response_digest(body),
                },
            )
        except asyncio.CancelledError as e:
            outcome = classify_submit_response(
                None, None, transport_started, e,
            )
            return _order_from_outcome(cid=cid, reference=reference, outcome=outcome)
        except httpx.HTTPError as e:
            log.warning("bitfinex_submit_network_error symbol=%s err_type=%s", decision.symbol, type(e).__name__)
            outcome = classify_submit_response(
                None, None, transport_started, e,
            )
            return _order_from_outcome(cid=cid, reference=reference, outcome=outcome)
        except Exception as e:
            # A response parser/shape error after the request is sent is also
            # UNKNOWN.  Never let it fall through to the old FAILED branch.
            log.warning("bitfinex_submit_response_error symbol=%s err_type=%s", decision.symbol, type(e).__name__)
            outcome = classify_submit_response(
                None, None, transport_started, e,
            )
            return _order_from_outcome(cid=cid, reference=reference, outcome=outcome)

        try:
            parsed_body = resp.json()
        except (TypeError, ValueError):
            outcome = SubmitOutcomeUnknown(
                reason="malformed_response",
                transport_started=True,
                raw_response_digest=response_digest(resp.text[:1000]),
            )
            return _order_from_outcome(
                cid=cid,
                reference=reference,
                outcome=outcome,
                raw_response={
                    "http_status": resp.status_code,
                    "response_digest": response_digest(resp.text[:1000]),
                },
            )
        outcome = classify_submit_response(
            resp.status_code,
            parsed_body,
            transport_started,
        )
        return _order_from_outcome(
            cid=cid,
            reference=reference,
            outcome=outcome,
        )

    async def cancel(
        self,
        *,
        venue_offer_id: str,
        signal_correlation_id: UUID,
        account_id: str,
        ctx: AccountContext,
    ) -> None:
        """Cancel an offer at Bitfinex.

        Event flow (3-event audit model — Phase 4.4b prework):
          1. publish CancelRequested (intent audit)
          2. POST /v2/auth/w/funding/offer/cancel with HMAC-SHA384 sign
          3. publish CancelAcknowledged (REST ack audit) on success or already-terminal
             — ledger/registry do NOT subscribe to this (audit-only)
          4. ws_dispatcher publishes ReservationReleased on WS `foc` (state mutation,
             owned by ws_dispatcher per single-SoT invariant — see spec §D2.5)

        Errors:
          - 401/403 → raise ExecutorAuthError → daemon exit 78
          - 5xx / network → transient_retry 3x (1s/2s/4s); exhaust → log warn + return
          - 200 ERROR "not found" / "not active" → already_terminal, log info +
            publish CancelAcknowledged(rest_status="already_terminal")
          - 200 ERROR other → log warn + do not publish CancelAcknowledged
          - other 4xx (ExecutorFatalError from classify_httpx_response) → log warn + return
        """
        # 1. Publish CancelRequested (intent audit)
        await self._bus.publish(CancelRequested(
            venue_offer_id=venue_offer_id,
            requested_at_ms=int(time.time() * 1000),
            signal_correlation_id=signal_correlation_id,
            account_id=account_id,
        ))

        # 2. POST cancel with transient retry
        wrapped = transient_retry(self._cancel_http_call)
        try:
            response_json = await wrapped(venue_offer_id, ctx)
        except ExecutorAuthError:
            raise  # propagate to daemon → exit 78
        except ExecutorTransientError as e:
            log.warning(
                "bitfinex_cancel_transient_exhausted voi=%s err=%r",
                venue_offer_id, e,
            )
            return
        except ExecutorFatalError as e:
            log.warning(
                "bitfinex_cancel_fatal voi=%s err=%r",
                venue_offer_id, e,
            )
            return

        # 3. Classify and publish CancelAcknowledged
        rest_status, text = classify_cancel_response(response_json)
        if rest_status in ("success", "already_terminal"):
            await self._bus.publish(CancelAcknowledged(
                venue_offer_id=venue_offer_id,
                acknowledged_at_ms=int(time.time() * 1000),
                signal_correlation_id=signal_correlation_id,
                account_id=account_id,
                rest_status=rest_status,
                venue_response_text=text,
            ))
        else:
            log.warning(
                "bitfinex_cancel_other_error voi=%s text=%s",
                venue_offer_id, text,
            )

    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: AccountContext,
    ) -> FundingCancelAllResult:
        """Cancel every funding offer in ``currency`` (see :class:`FundingCancelAllClient`)."""
        return await FundingCancelAllClient(
            http=self._http, nonce_provider=self._nonce_provider, base_url=self._base_url,
        ).cancel_all_funding_offers(currency=currency, ctx=ctx)

    async def _cancel_http_call(
        self,
        venue_offer_id: str,
        ctx: AccountContext,
    ) -> Any:
        """Pure I/O — HTTP POST + classify response into typed exception.

        Returns Bitfinex response JSON on 2xx; raises ExecutorAuthError /
        ExecutorFatalError / ExecutorTransientError on 4xx/5xx/network.
        """
        if ctx.before_cancel_transport is not None:
            await ctx.before_cancel_transport()
        try:
            voi_int = int(venue_offer_id)
        except ValueError as e:
            raise ExecutorFatalError(
                f"venue_offer_id not numeric: {venue_offer_id!r}",
            ) from e
        body = {"id": voi_int}
        body_bytes = json.dumps(body).encode("utf-8")
        nonce = self._nonce_provider()
        headers = sign_request(
            body=body_bytes, nonce=nonce,
            api_secret=ctx.credentials.api_secret,
            path=_OFFER_CANCEL_PATH,
        )
        headers["bfx-apikey"] = ctx.credentials.api_key
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                f"{self._base_url}/{_OFFER_CANCEL_PATH}",
                content=body_bytes, headers=headers,
            )
        except httpx.HTTPError as exc:
            raise classify_httpx_exception(exc) from exc
        if resp.status_code >= 400:
            raise classify_httpx_response(resp)
        return resp.json()
