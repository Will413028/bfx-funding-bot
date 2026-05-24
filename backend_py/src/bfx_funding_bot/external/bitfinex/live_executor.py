"""BitfinexLiveExecutor — REST submit + cancel (Phase 4.4a).

Pure fns (build_offer_payload, parse_offer_response) + I/O shell (Task 14).

Per spec §6.3:
  - submit returns status="submitted" (NOT "filled") — WS fcn fills later
  - submit failure → status="failed", venue_offer_id=None
  - cancel publishes CancelRequested event (no in-memory _pending_cancels dict)
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any, Protocol
from uuid import UUID

import httpx

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.retry import (
    classify_httpx_exception,
    classify_httpx_response,
    transient_retry,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

BITFINEX_REST_BASE = "https://api.bitfinex.com"
_OFFER_SUBMIT_PATH = "v2/auth/w/funding/offer/submit"
_OFFER_CANCEL_PATH = "v2/auth/w/funding/offer/cancel"


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


def classify_cancel_response(raw: Any) -> tuple[str, str | None]:
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


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class BitfinexLiveExecutor:
    """Bitfinex REST funding offer executor.

    Pure REST — no WS, no Registry dependency. submit returns status="submitted";
    WS fcn (handled by BitfinexLiveWSDispatcher) publishes OrderFilled later.

    cancel publishes CancelRequested event (first-class) — replaces former
    _pending_cancels dict pattern.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        axiom: _AxiomProtocol,
        bus: DomainEventBus,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
        nonce_provider: Callable[[], int] | None = None,
        date_provider: Callable[[], date] | None = None,
        base_url: str = BITFINEX_REST_BASE,
    ) -> None:
        self._http = http
        self._axiom = axiom
        self._bus = bus
        self._phase = phase
        self._strategy = strategy
        self._cell = cell
        self._nonce_provider = nonce_provider or (lambda: int(time.time() * 1_000_000))
        self._date_provider = date_provider or (lambda: date.today())
        self._base_url = base_url

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        # cid centralized by ReservationEmittingMiddleware (A2); direct callers
        # fall back to deterministic generation (CC2 capture-once date).
        if cid is None:
            cid = generate_cid(decision.signal_correlation_id, self._date_provider())
        payload = build_offer_payload(
            symbol="fUSD",
            amount_usdt=decision.offer_amount_usdt or 0.0,
            rate=decision.offer_rate or 0.0,
            period_days=decision.offer_duration_days or 2,
            cid=cid,
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

        try:
            resp = await self._http.post(
                f"{self._base_url}/{_OFFER_SUBMIT_PATH}",
                content=body_bytes, headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("bitfinex_submit_http_error err=%r", e)
            return SubmittedOrder(cid=cid, venue_offer_id=None, status="failed", raw_response=None)

        parsed = parse_offer_response(resp.json())
        return replace(parsed, cid=cid)

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

    async def _cancel_http_call(
        self,
        venue_offer_id: str,
        ctx: AccountContext,
    ) -> Any:
        """Pure I/O — HTTP POST + classify response into typed exception.

        Returns Bitfinex response JSON on 2xx; raises ExecutorAuthError /
        ExecutorFatalError / ExecutorTransientError on 4xx/5xx/network.
        """
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
