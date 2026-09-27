"""Integration test: cancel REST path with real httpx client + respx mocks.

Differs from unit tests by using real httpx.AsyncClient (no patches), tests
the full sign_request + nonce handling on the wire.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from httpx import Response

from bfx_funding_bot.external.bitfinex.live_executor import (
    BITFINEX_REST_BASE,
    BitfinexLiveExecutor,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
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

pytestmark = pytest.mark.integration


class _RecordingSink:
    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_cancel_success_full_chain_sends_signed_post() -> None:
    """Verify HMAC-SHA384 signed POST reaches Bitfinex; bus emits 2 events."""
    bus = DomainEventBus()
    captured: list[Any] = []

    async def _capture(event: Any) -> None:
        captured.append(event)

    bus.subscribe(CancelRequested, _capture)
    bus.subscribe(CancelAcknowledged, _capture)

    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="test-key", api_secret="test-secret"),
        allocation_cap_usdt=Decimal("500"),
    )

    async with httpx.AsyncClient() as http:
        executor = BitfinexLiveExecutor(
            http=http,
            event_sink=_RecordingSink(),
            bus=bus,
            phase=Phase.PAPER,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUSD"}),
            cell="fUSD_p2",
            auth_gate=AuthRequestGate(lambda: 1700000000_000_000),
            date_provider=lambda: date(2026, 5, 23),
        )

        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=[
                    1700000000000, "foc-req", None, None,
                    ["123", "fUSD", "rate", "amount"],
                    "0", "SUCCESS", None, "Submitting cancel request",
                ]),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=ctx,
            )

    # Verify request was signed
    last_request = route.calls.last.request
    assert last_request.headers["bfx-apikey"] == "test-key"
    assert "bfx-nonce" in last_request.headers
    assert "bfx-signature" in last_request.headers
    body = last_request.read().decode("utf-8")
    # json.dumps({"id": 123}) with Python defaults → '{"id": 123}' (space after colon)
    assert '"id": 123' in body

    # Verify 2 events published
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested", "CancelAcknowledged"]
