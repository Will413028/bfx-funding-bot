"""ReservationEmittingMiddleware — publish ReservationClaimed + OrderFilled (paper)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)


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


class _StubInner:
    def __init__(self, result: SubmittedOrder | BaseException) -> None:
        self._result = result

    async def submit(self, decision: DecisionPayload, ctx: AccountContext) -> SubmittedOrder:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _make_bus_capture() -> tuple[DomainEventBus, list[ReservationClaimed], list[OrderFilled]]:
    bus = DomainEventBus()
    claims: list[ReservationClaimed] = []
    fills: list[OrderFilled] = []
    async def on_claim(e: ReservationClaimed) -> None:
        claims.append(e)
    async def on_fill(e: OrderFilled) -> None:
        fills.append(e)
    bus.subscribe(ReservationClaimed, on_claim)
    bus.subscribe(OrderFilled, on_fill)
    return bus, claims, fills


@pytest.mark.asyncio
async def test_paper_filled_emits_claim_then_fill() -> None:
    bus, claims, fills = _make_bus_capture()
    inner = _StubInner(SubmittedOrder(
        cid=42, venue_offer_id="paper_abc", status="filled", raw_response=None,
    ))
    mw = ReservationEmittingMiddleware(inner, bus=bus)
    await mw.submit(_decision(), _ctx())
    assert len(claims) == 1
    assert len(fills) == 1
    assert claims[0].cid == 42
    assert claims[0].venue_offer_id == "paper_abc"
    assert claims[0].size_usdt == Decimal("100.0")
    assert fills[0].cid == 42


@pytest.mark.asyncio
async def test_live_submitted_emits_claim_only() -> None:
    """Live BitfinexLive returns status='submitted' — Reserved emitted, Fill 不發."""
    bus, claims, fills = _make_bus_capture()
    inner = _StubInner(SubmittedOrder(
        cid=42, venue_offer_id="123456", status="submitted", raw_response=None,
    ))
    mw = ReservationEmittingMiddleware(inner, bus=bus)
    await mw.submit(_decision(), _ctx())
    assert len(claims) == 1
    assert len(fills) == 0


@pytest.mark.asyncio
async def test_failed_status_emits_nothing() -> None:
    """I3-EM: status=failed → 不 emit 任何 event (防 ledger 漏洞)."""
    bus, claims, fills = _make_bus_capture()
    inner = _StubInner(SubmittedOrder(
        cid=42, venue_offer_id=None, status="failed", raw_response=None,
    ))
    mw = ReservationEmittingMiddleware(inner, bus=bus)
    await mw.submit(_decision(), _ctx())
    assert len(claims) == 0
    assert len(fills) == 0


@pytest.mark.asyncio
async def test_inner_raise_propagates_no_emit() -> None:
    bus, claims, fills = _make_bus_capture()
    inner = _StubInner(ExecutorTransientError("blip"))
    mw = ReservationEmittingMiddleware(inner, bus=bus)
    with pytest.raises(ExecutorTransientError):
        await mw.submit(_decision(), _ctx())
    assert len(claims) == 0
    assert len(fills) == 0


@pytest.mark.asyncio
async def test_bus_publish_failure_does_not_break_submit() -> None:
    """I4-EM: bus.publish 例外 swallow，submit 仍 return result.

    Note: DomainEventBus.publish already isolates handler exceptions via
    gather(return_exceptions=True). This test verifies the outer try/except
    in middleware swallows even publish-level (not handler-level) failures.
    Use a broken bus to force this path.
    """
    class _BrokenBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus publish broken")
    inner = _StubInner(SubmittedOrder(
        cid=42, venue_offer_id="paper_abc", status="filled", raw_response=None,
    ))
    mw = ReservationEmittingMiddleware(inner, bus=_BrokenBus())  # type: ignore[arg-type]
    result = await mw.submit(_decision(), _ctx())
    assert result.status == "filled"


@pytest.mark.asyncio
async def test_decimal_conversion_from_decision() -> None:
    bus, claims, _ = _make_bus_capture()
    inner = _StubInner(SubmittedOrder(
        cid=1, venue_offer_id="x", status="submitted", raw_response=None,
    ))
    mw = ReservationEmittingMiddleware(inner, bus=bus)
    d = DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=12345.67, offer_duration_days=2,
    )
    await mw.submit(d, _ctx())
    assert claims[0].size_usdt == Decimal("12345.67")
