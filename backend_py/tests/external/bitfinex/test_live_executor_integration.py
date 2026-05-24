from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import CancelRequested
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)


def _make_decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0005,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    )


def _make_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.asyncio
async def test_submit_returns_submitted_on_success() -> None:
    success_response = [
        1716383500000, "fon-req", None, None,
        [42, "fUSD", 0, 0, 100.0, 0, "REQ", None, None, 0, "ACTIVE",
         None, None, None, 0.0005, 2, 0, 0, None, 0, None, None, None, 12345],
        None, "SUCCESS", None, "Submitting",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=success_response)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bus = DomainEventBus()
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )

    result = await executor.submit(_make_decision(), _make_ctx())
    assert result.status == "submitted"
    assert result.venue_offer_id == "42"


@pytest.mark.asyncio
async def test_submit_returns_failed_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bus = DomainEventBus()
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )
    result = await executor.submit(_make_decision(), _make_ctx())
    assert result.status == "failed"
    assert result.venue_offer_id is None


@pytest.mark.asyncio
async def test_cancel_publishes_cancel_requested() -> None:
    # Bitfinex cancel SUCCESS shape (10 elements; raw[6]="SUCCESS")
    cancel_success_resp = [
        1700000000000, "foc-req", None, None,
        ["42", "fUSD", "rate", "amount"],
        "0", "SUCCESS", None, "Submitting cancel request",
    ]
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=cancel_success_resp)
    ))
    bus = DomainEventBus(clock=lambda: 5000)
    captured: list = []

    async def capture(ev: CancelRequested) -> None:
        captured.append(ev)
    bus.subscribe(CancelRequested, capture)

    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )

    sig_id = uuid4()
    await executor.cancel(
        venue_offer_id="42",
        signal_correlation_id=sig_id,
        account_id="default",
        ctx=_make_ctx(),
    )

    assert len(captured) == 1
    assert captured[0].venue_offer_id == "42"
    assert captured[0].signal_correlation_id == sig_id
    assert captured[0].requested_at_ms > 0
