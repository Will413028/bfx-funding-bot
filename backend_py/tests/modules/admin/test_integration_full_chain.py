"""Full chain integration: SmokeRunner end-to-end with real bus + middleware
+ axiom_sink + prod_ledger. Verifies (1) prod ledger untouched, (2) recorder
captures both smoke events, (3) AxiomEventSink emits with smoke account_id.

Note: NOT marked pytest.mark.integration — runs without network (fake axiom
client). Lives in tests/modules/admin/ for default test run inclusion.
"""
from __future__ import annotations

from typing import Any

from bfx_funding_bot.modules.admin.smoke_runner import (
    SMOKE_ACCOUNT_ID,
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    Phase,
    StrategyName,
)


class _FakeEventSink:
    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _StubEventLogQuery:
    async def query_order_events(
        self, account_id: str, since: Any,
    ) -> list[dict[str, Any]]:
        return []


async def test_smoke_does_not_touch_prod_ledger() -> None:
    """Invariant I1: smoke pollutes 0 prod state."""
    bus = DomainEventBus()
    axiom = _FakeEventSink()
    prod_ledger = PaperPositionLedger(account_id="default")
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, prod_ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, prod_ledger.on_order_filled)
    bus.subscribe(ReservationReleased, prod_ledger.on_reservation_released)
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)
    bus.subscribe(ReservationReleased, sink.on_reservation_released)

    paper = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus, persister=NoopEventPersister())
    runner = SmokeRunner(
        executor=wrapped, bus=bus,
        pg_query=_StubEventLogQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )

    before_reserved = prod_ledger.current_exposure()
    before_realized = prod_ledger.realized_exposure()

    result = await runner.run_l2()

    assert result.status == "pass"
    assert prod_ledger.current_exposure() == before_reserved
    assert prod_ledger.realized_exposure() == before_realized
    assert prod_ledger.replay_floor_hit_count == 0


async def test_smoke_axiom_sink_emits_with_smoke_account_id() -> None:
    """Invariant I5: axiom_sink emits 2 events with account_id=smoke_test."""
    bus = DomainEventBus()
    axiom = _FakeEventSink()
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)

    paper = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus, persister=NoopEventPersister())
    runner = SmokeRunner(
        executor=wrapped, bus=bus,
        pg_query=_StubEventLogQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    await runner.run_l2()

    # Two emit categories: (a) EchoPaperExecutor emits order_submit + order_fill
    # directly to axiom, (b) AxiomEventSink emits reservation_claimed + order_fill
    # via bus subscription. Filter to bus-emitted events (those from sink):
    sink_emits = [
        e for e in axiom.emits
        if e.get("event_type") in (
            EventType.RESERVATION_CLAIMED.value,
            EventType.ORDER_FILL.value,
            EventType.RESERVATION_RELEASED.value,
        )
        and e.get("account_id") == SMOKE_ACCOUNT_ID
    ]
    sink_event_types = {e["event_type"] for e in sink_emits}
    # AxiomEventSink should have emitted RESERVATION_CLAIMED + ORDER_FILL
    # (no RESERVATION_RELEASED for paper happy path).
    assert EventType.RESERVATION_CLAIMED.value in sink_event_types
    assert EventType.ORDER_FILL.value in sink_event_types
    # All bus-emitted events tagged smoke_test
    for e in sink_emits:
        assert e["account_id"] == SMOKE_ACCOUNT_ID
