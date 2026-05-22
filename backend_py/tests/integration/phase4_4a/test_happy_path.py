"""Phase 4.4a integration: submit → ReservationClaimed → fcn → OrderFilled E2E."""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import FcnEvent
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.events import ReservationClaimed
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


def _success_response(venue_offer_id: str = "42") -> list[Any]:
    return [
        1716383500000, "fon-req", None, None,
        [int(venue_offer_id), "fUSD", 0, 0, 100.0, 0, "REQ", None, None,
         0, "ACTIVE", None, None, None, 0.0005, 2, 0, 0, None, 0, None, None, None, 12345],
        None, "SUCCESS", None, "Submitting",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_then_fcn_completes_orderfilled_chain(
    domain_chain: dict[str, Any],
    axiom_sink: Any,
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response(voi))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _DummyAxiom:
        async def emit(self, event: dict[str, Any]) -> None:
            pass

    executor = BitfinexLiveExecutor(
        http=http, axiom=_DummyAxiom(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )

    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=sig_id,
        offer_rate=0.0005,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    )
    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )

    # 1. Submit returns "submitted"
    submitted = await executor.submit(decision, ctx)
    assert submitted.status == "submitted"
    assert submitted.venue_offer_id == voi

    # 2. Middleware would publish ReservationClaimed — simulate here
    await bus.publish(ReservationClaimed(
        cid=submitted.cid, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    ))

    assert ledger.current_exposure() == Decimal("100")
    assert registry.snapshot()[voi].state.value == "claimed"

    # 3. WS pushes fcn — dispatcher translates to OrderFilled
    fcn = FcnEvent(
        credit_id=999, symbol="fUSD", side=1,
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2,
        offer_id_meta=42, raw_seq=5, raw=[],
    )

    from .conftest import ScriptedWSClient
    fake_ws = ScriptedWSClient([fcn])

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        axiom=_DummyAxiom(), clock=lambda: 2500, queue_max=100,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # 4. Assertions: ledger / registry / axiom rows
    assert ledger.realized_exposure() == Decimal("100")
    assert ledger.current_exposure() == Decimal("100")  # 0 reserved + 100 realized
    assert registry.snapshot()[voi].state.value == "released"

    fill_rows = [r for r in axiom_sink.rows if r["event_type"] == "OrderFilled"]
    assert len(fill_rows) == 1
    assert fill_rows[0]["credit_id"] == "999"
    assert fill_rows[0]["event_seq"] is not None
    assert fill_rows[0]["occurred_at_ms"] == 2000
