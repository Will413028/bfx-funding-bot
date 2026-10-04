"""Phase 4.4a integration: submit → ReservationClaimed → foc EXECUTED → OrderFilled E2E."""
from __future__ import annotations

import asyncio
import time
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.auth_ws import FocEvent
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.execution.legacy_venue_hints import LegacyVenueHintSink
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload, StrategyName

from .conftest import ScriptedWSClient, make_reservation_ref


def _success_response(venue_offer_id: str = "42") -> list[Any]:
    return [
        1716383500000, "fon-req", None, None,
        [int(venue_offer_id), "fUSD", 0, 0, 150.0, 0, "REQ", None, None,
         0, "ACTIVE", None, None, None, 0.0005, 2, 0, 0, None, 0, None, None, None, 12345],
        None, "SUCCESS", "Submitting",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_then_foc_executed_completes_orderfilled_chain(
    domain_chain: dict[str, Any],
    event_sink_stub: Any,
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]

    sig_id = uuid4()
    voi = "42"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response(voi))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _EventCapture:
        async def emit(self, event: dict[str, Any]) -> None:
            pass

    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.SHADOW, strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUSD"}), cell="C-1",
        auth_gate=AuthRequestGate(lambda: 1000),
        date_provider=lambda: date(2026, 5, 22),
    )

    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=sig_id,
        offer_rate=0.0005,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
    symbol="fUSD")
    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )

    # 1. Submit returns "submitted"
    from tests.external.bitfinex.test_funding_rules import evidence
    ready = ReadyToSubmit(
        decision=decision,
        decision_id="phase4-4a-happy-path",
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="phase4-4a-test-snapshot",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="integration-test"),
        funding_amount_evidence=evidence(symbol="fUSD", now=int(time.time() * 1000)),
    )
    submitted = await executor.submit(ready, ctx)
    assert submitted.status == "submitted"
    assert submitted.venue_offer_id == voi

    # 2. Middleware would publish ReservationClaimed — simulate here
    await bus.publish(ReservationClaimed(
        cid=submitted.cid, venue_offer_id=voi, size_usdt=Decimal("150"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
        symbol="fUSD",
        reservation_ref=make_reservation_ref(submitted.cid, sig_id, voi),
    ))

    assert ledger.current_exposure("fUSD") == Decimal("150")
    assert registry.snapshot()[voi].state.value == "claimed"

    # 3. WS pushes foc EXECUTED — dispatcher translates to OrderFilled
    foc = FocEvent(
        venue_offer_id=voi, symbol="fUSD",
        mts_create=2000, mts_update=2000,
        amount=Decimal("150"), status="EXECUTED @ 0.0005 (150.0)",
        rate=0.0005, period_days=2, raw_seq=5, raw=[],
    )

    fake_ws = ScriptedWSClient([foc])

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws,
        event_sink=_EventCapture(), clock=lambda: 2500, queue_max=100, venue_hint_sink=LegacyVenueHintSink(registry=registry, bus=bus, persister=NoopEventPersister(), account_id=None))

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # 4. Assertions: ledger / registry / event rows
    assert ledger.realized_exposure("fUSD") == Decimal("150")
    assert ledger.current_exposure("fUSD") == Decimal("150")  # 0 reserved + 150 realized
    assert registry.snapshot()[voi].state.value == "released"

    fill_rows = [r for r in event_sink_stub.rows if r["event_type"] == "OrderFilled"]
    assert len(fill_rows) == 1
    assert fill_rows[0]["credit_id"] is None  # foc carries no credit id
    assert fill_rows[0]["event_seq"] is not None
    assert fill_rows[0]["occurred_at_ms"] == 2000
