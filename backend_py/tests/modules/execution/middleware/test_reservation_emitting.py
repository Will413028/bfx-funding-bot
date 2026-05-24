"""ReservationEmittingMiddleware — A2 write-ahead intent + sync persistence."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


class _RecordingPersister:
    """Records each persist() call as one tuple of events (= one txn)."""
    def __init__(self) -> None:
        self.txns: list[tuple[object, ...]] = []

    async def persist(self, *events: object) -> None:
        self.txns.append(events)


class _StubInner:
    """Echoes the injected cid back in the SubmittedOrder (A2 contract)."""
    def __init__(self, status: str, voi: str | None, *, persister: _RecordingPersister | None = None) -> None:
        self._status = status
        self._voi = voi
        self._persister = persister
        self.persist_calls_at_submit: int | None = None
        self.cid_seen: int | None = None

    async def submit(self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None) -> SubmittedOrder:
        self.cid_seen = cid
        if self._persister is not None:
            self.persist_calls_at_submit = len(self._persister.txns)
        return SubmittedOrder(cid=cid or 0, venue_offer_id=self._voi, status=self._status, raw_response=None)


def _bus_capture() -> tuple[DomainEventBus, list[object]]:
    bus = DomainEventBus()
    seen: list[object] = []
    async def _on(e: object) -> None:
        seen.append(e)
    bus.subscribe(ReservationClaimed, _on)
    bus.subscribe(OrderFilled, _on)
    return bus, seen


@pytest.mark.asyncio
async def test_paper_filled_persists_intent_then_claim_and_fill() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_abc", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=True)
    await mw.submit(_decision(), _ctx())
    assert len(persister.txns) == 2
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationClaimed, OrderFilled]
    assert inner.persist_calls_at_submit == 1
    assert [type(e) for e in seen] == [ReservationClaimed, OrderFilled]


@pytest.mark.asyncio
async def test_live_submitted_persists_intent_then_claim_only() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("submitted", "123456", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=False)
    await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationClaimed]
    assert [type(e) for e in seen] == [ReservationClaimed]
    assert persister.txns[1][0].is_simulated is False


@pytest.mark.asyncio
async def test_failed_persists_intent_then_failed_no_publish() -> None:
    bus, seen = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("failed", None, persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=False)
    await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert [type(e) for e in persister.txns[1]] == [ReservationFailed]
    assert seen == []


@pytest.mark.asyncio
async def test_same_cid_threaded_to_inner_and_all_events() -> None:
    bus, _ = _bus_capture()
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_x", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=bus, persister=persister, is_simulated=True)
    await mw.submit(_decision(), _ctx())
    intent_cid = persister.txns[0][0].cid
    claim_cid = persister.txns[1][0].cid
    assert inner.cid_seen == intent_cid == claim_cid


@pytest.mark.asyncio
async def test_bus_publish_failure_does_not_break_submit() -> None:
    class _BrokenBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus publish broken")
    persister = _RecordingPersister()
    inner = _StubInner("filled", "paper_abc", persister=persister)
    mw = ReservationEmittingMiddleware(inner, bus=_BrokenBus(), persister=persister, is_simulated=True)  # type: ignore[arg-type]
    result = await mw.submit(_decision(), _ctx())
    assert result.status == "filled"


@pytest.mark.asyncio
async def test_inner_raise_after_intent_propagates() -> None:
    class _RaisingInner:
        async def submit(self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None) -> SubmittedOrder:
            raise ExecutorTransientError("blip")
    bus, _ = _bus_capture()
    persister = _RecordingPersister()
    mw = ReservationEmittingMiddleware(_RaisingInner(), bus=bus, persister=persister, is_simulated=True)
    with pytest.raises(ExecutorTransientError):
        await mw.submit(_decision(), _ctx())
    assert [type(e) for e in persister.txns[0]] == [ReservationIntent]
    assert len(persister.txns) == 1
