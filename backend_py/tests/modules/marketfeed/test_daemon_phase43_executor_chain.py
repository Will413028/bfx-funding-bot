"""Phase 4.3 executor-middleware integration: chain composition + end-to-end
ledger / axiom emit / sad path subscriber isolation.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

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
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
    TransientRetryMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)


class _CapturingAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _PaperInner:
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        return SubmittedOrder(
            cid=42,
            venue_offer_id="paper_xyz",
            status="filled",
            raw_response=None,
        )


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _build_chain(
    axiom: _CapturingAxiom,
    ledger: PaperPositionLedger,
) -> tuple[HeartbeatMiddleware, HealthProbe, DomainEventBus]:
    bus = DomainEventBus()
    probe = HealthProbe()
    sink = AxiomEventSink(
        axiom_client=axiom,
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)
    bus.subscribe(ReservationReleased, sink.on_reservation_released)
    executor = HeartbeatMiddleware(
        ReservationEmittingMiddleware(
            TransientRetryMiddleware(_PaperInner()), bus=bus,
            persister=NoopEventPersister(),
        ),
        probe=probe,
    )
    return executor, probe, bus


@pytest.mark.asyncio
async def test_wired_chain_types() -> None:
    ledger = PaperPositionLedger(account_id="default")
    axiom = _CapturingAxiom()
    executor, _probe, _bus = _build_chain(axiom, ledger)
    assert isinstance(executor, HeartbeatMiddleware)
    inner1 = executor._inner  # type: ignore[attr-defined]
    assert isinstance(inner1, ReservationEmittingMiddleware)
    inner2 = inner1._inner  # type: ignore[attr-defined]
    assert isinstance(inner2, TransientRetryMiddleware)


@pytest.mark.asyncio
async def test_paper_end_to_end_ledger_axiom_heartbeat() -> None:
    """Happy path: submit one paper order, verify ledger / axiom / heartbeat."""
    ledger = PaperPositionLedger(account_id="default")
    axiom = _CapturingAxiom()
    executor, probe, _bus = _build_chain(axiom, ledger)

    result = await executor.submit(_decision(), _ctx())

    assert result.status == "filled"
    # Ledger: paper CLAIMED + FILLED back-to-back → reserved=0, realized=100
    assert ledger.current_exposure() == Decimal("100")
    assert ledger.realized_exposure() == Decimal("100")
    # Axiom: 2 events (CLAIMED, FILL)
    event_types = [e["event_type"] for e in axiom.events]
    assert event_types == ["reservation_claimed", "order_fill"]
    # Heartbeat fired
    assert probe.last_active_ts.get("executor") is not None


@pytest.mark.asyncio
async def test_fill_tracker_emits_release_via_bus_reduces_ledger() -> None:
    """fill_tracker emit ReservationReleased → ledger reserved -=."""
    ledger = PaperPositionLedger(account_id="default")
    axiom = _CapturingAxiom()
    executor, _probe, bus = _build_chain(axiom, ledger)

    # Submit once to create the paper sync claim+fill (reserved goes to 0, realized 100)
    await executor.submit(_decision(), _ctx())
    # Then simulate fill_tracker observing offer disappear → emit RELEASE
    # Since paper FILL already 0'd reserved, RELEASE triggers floor (count++)
    await bus.publish(
        ReservationReleased(
            cid=42,
            venue_offer_id="paper_xyz",
            size_usdt=Decimal("100"),
            reason="missing_from_venue",
            signal_correlation_id=uuid4(),
            account_id="default",
            is_simulated=False,
        )
    )
    assert ledger.replay_floor_hit_count == 1


@pytest.mark.asyncio
async def test_sad_path_axiom_failure_does_not_break_ledger() -> None:
    """I4-EM + I6-Bus: axiom_sink failure 不影響 ledger subscriber."""
    ledger = PaperPositionLedger(account_id="default")

    class _BrokenAxiom:
        async def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("axiom client final give-up")

    bus = DomainEventBus()
    probe = HealthProbe()
    sink = AxiomEventSink(
        axiom_client=_BrokenAxiom(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)

    executor = HeartbeatMiddleware(
        ReservationEmittingMiddleware(
            TransientRetryMiddleware(_PaperInner()), bus=bus,
            persister=NoopEventPersister(),
        ),
        probe=probe,
    )

    result = await executor.submit(_decision(), _ctx())  # 不 raise
    assert result.status == "filled"
    # Ledger 仍正確 (axiom 失敗不影響 — bus.gather isolates subscribers)
    assert ledger.realized_exposure() == Decimal("100")
