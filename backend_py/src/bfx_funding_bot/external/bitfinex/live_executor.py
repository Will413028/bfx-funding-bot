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

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.events import CancelRequested
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

BITFINEX_REST_BASE = "https://api.bitfinex.com"
_OFFER_SUBMIT_PATH = "v2/auth/w/funding/offer/submit"


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
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
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
        self, *, venue_offer_id: str,
        signal_correlation_id: UUID, account_id: str,
    ) -> None:
        """Publish CancelRequested event.

        Per spec §6.3: REST cancel call deferred to follow-up wire-up; for 4.4a
        the primary goal is bus.publish(CancelRequested) — making cancel
        auditable + replay-able as a first-class event.
        """
        await self._bus.publish(CancelRequested(
            venue_offer_id=venue_offer_id,
            requested_at_ms=int(time.time() * 1000),
            signal_correlation_id=signal_correlation_id,
            account_id=account_id,
        ))
