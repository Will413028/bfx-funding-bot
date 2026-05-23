"""Unit tests for BitfinexLiveExecutor.cancel — pure fns + I/O shell."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from httpx import Response

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.live_executor import (
    BITFINEX_REST_BASE,
    BitfinexLiveExecutor,
    classify_cancel_response,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


def test_classify_success() -> None:
    raw = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],  # offer_array placeholder
        "0",
        "SUCCESS",
        None,
        "Submitting cancel request",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "success"
    assert text == "Submitting cancel request"


def test_classify_already_terminal_not_found() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Offer not found.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "already_terminal"
    assert text == "Offer not found."


def test_classify_already_terminal_not_active() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Offer is not active.",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "already_terminal"


def test_classify_other_error() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None,
        "Internal error.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "other"
    assert text == "Internal error."


def test_classify_failure_status_treated_as_other() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "FAILURE", None,
        "Unknown failure",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "other"


def test_classify_malformed_response_raises() -> None:
    from bfx_funding_bot.modules.execution.errors import InvariantViolation
    with pytest.raises(InvariantViolation):
        classify_cancel_response({"not": "a list"})
    with pytest.raises(InvariantViolation):
        classify_cancel_response([1, 2])  # too short


# ----------------------------------------------------------------------------
# I/O shell tests (Task 9): cancel() REST integration
# ----------------------------------------------------------------------------


def _make_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="test-key", api_secret="test-secret"),
        allocation_cap_usdt=Decimal("500"),
    )


def _make_executor(bus: DomainEventBus, http: httpx.AsyncClient) -> BitfinexLiveExecutor:
    class _Axiom:
        async def emit(self, event: dict[str, Any]) -> None:
            return None

    return BitfinexLiveExecutor(
        http=http,
        axiom=_Axiom(),
        bus=bus,
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="fUSD_p2",
        nonce_provider=lambda: 1700000000_000_000,
        date_provider=lambda: date(2026, 5, 23),
    )


def _capture(events: list[Any]) -> Any:
    async def _h(ev: Any) -> None:
        events.append(ev)
    return _h


@pytest.fixture
def _no_tenacity_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep to avoid 7s wall-time on retry-exhaustion tests.

    tenacity's _portable_async_sleep lazy-imports asyncio and calls asyncio.sleep
    at runtime, so module-level monkeypatch is sufficient.
    """
    import asyncio as _asyncio

    async def _instant(_seconds: float) -> None:
        return None
    monkeypatch.setattr(_asyncio, "sleep", _instant)


async def test_cancel_success_publishes_requested_then_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    success_resp = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],
        "0", "SUCCESS", None, "Submitting cancel request",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=success_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested", "CancelAcknowledged"]
    ack = captured[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "success"
    assert ack.venue_offer_id == "123"


async def test_cancel_already_terminal_still_publishes_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    error_resp = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None, "Offer not found.",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=error_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested", "CancelAcknowledged"]
    ack = captured[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "already_terminal"


async def test_cancel_auth_error_raises_executor_auth_error() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(401, json={"error": "auth"}),
            )
            with pytest.raises(ExecutorAuthError):
                await executor.cancel(
                    venue_offer_id="123",
                    signal_correlation_id=uuid4(),
                    account_id="default",
                    ctx=_make_ctx(),
                )

    # Intent was published before error
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]


async def test_cancel_5xx_retries_then_swallows(_no_tenacity_sleep: None) -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(503, json={"error": "down"}),
            )
            # Should NOT raise — log warn + return
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )
        # transient_retry RETRY_ATTEMPTS=3 → called 3 times
        assert route.call_count == 3

    # Only CancelRequested fired (no Acknowledged)
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]


async def test_cancel_other_error_logs_no_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    error_resp = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", None, "Internal error.",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=error_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    # Only CancelRequested fired (no Acknowledged for "other")
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]
