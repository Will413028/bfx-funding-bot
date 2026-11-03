"""Integration test: cancel REST path with real httpx client + respx mocks.

Differs from unit tests by using real httpx.AsyncClient (no patches), tests
the full sign_request + nonce handling on the wire. The account command gate
runs over the real adapter and publishes the cancel audit events around it.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from bfx_funding_bot.external.bitfinex.live_executor import BITFINEX_REST_BASE
from tests.external.bitfinex.test_live_executor_cancel import _CancelHarness

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_cancel_success_full_chain_sends_signed_post() -> None:
    """Verify HMAC-SHA384 signed POST reaches Bitfinex; the gate's bus emits 2 events."""
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)

        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=[
                    1700000000000, "foc-req", None, None,
                    ["123", "fUSD", "rate", "amount"],
                    "0", "SUCCESS", "Submitting cancel request",
                ]),
            )
            await harness.cancel()

    # Verify request was signed
    last_request = route.calls.last.request
    assert last_request.headers["bfx-apikey"] == "test-key"
    assert "bfx-nonce" in last_request.headers
    assert "bfx-signature" in last_request.headers
    body = last_request.read().decode("utf-8")
    # json.dumps({"id": 123}) with Python defaults → '{"id": 123}' (space after colon)
    assert '"id": 123' in body

    # Verify 2 events published
    assert harness.types == ["CancelRequested", "CancelAcknowledged"]
