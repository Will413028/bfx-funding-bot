"""DomainEventBus — in-process async pub/sub with handler isolation."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


def _make_claim() -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_safe() -> None:
    bus = DomainEventBus()
    await bus.publish(_make_claim())  # no raise


@pytest.mark.asyncio
async def test_single_subscriber_receives_event() -> None:
    bus = DomainEventBus()
    received: list[ReservationClaimed] = []
    async def handler(event: ReservationClaimed) -> None:
        received.append(event)
    bus.subscribe(ReservationClaimed, handler)
    claim = _make_claim()
    await bus.publish(claim)
    # Bus attaches event_seq and recorded_at_ms; verify core fields match.
    assert len(received) == 1
    assert received[0].cid == claim.cid
    assert received[0].venue_offer_id == claim.venue_offer_id
    assert received[0].size_usdt == claim.size_usdt
    assert received[0].event_seq == 1  # Bus attaches monotonic seq
    assert received[0].recorded_at_ms is not None  # Bus attaches clock time


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive() -> None:
    bus = DomainEventBus()
    counts = [0, 0, 0]
    async def make_handler(idx: int):
        async def h(event: ReservationClaimed) -> None:
            counts[idx] += 1
        return h
    bus.subscribe(ReservationClaimed, await make_handler(0))
    bus.subscribe(ReservationClaimed, await make_handler(1))
    bus.subscribe(ReservationClaimed, await make_handler(2))
    await bus.publish(_make_claim())
    assert counts == [1, 1, 1]


@pytest.mark.asyncio
async def test_handler_exception_does_not_affect_other_handlers() -> None:
    bus = DomainEventBus()
    survivor_called = False
    async def raiser(event: ReservationClaimed) -> None:
        raise RuntimeError("boom")
    async def survivor(event: ReservationClaimed) -> None:
        nonlocal survivor_called
        survivor_called = True
    bus.subscribe(ReservationClaimed, raiser)
    bus.subscribe(ReservationClaimed, survivor)
    await bus.publish(_make_claim())  # no raise — gather isolates
    assert survivor_called


@pytest.mark.asyncio
async def test_event_type_isolation() -> None:
    bus = DomainEventBus()
    claim_received: list[ReservationClaimed] = []
    fill_received: list[OrderFilled] = []
    async def claim_handler(e: ReservationClaimed) -> None:
        claim_received.append(e)
    async def fill_handler(e: OrderFilled) -> None:
        fill_received.append(e)
    bus.subscribe(ReservationClaimed, claim_handler)
    bus.subscribe(OrderFilled, fill_handler)
    await bus.publish(_make_claim())
    assert len(claim_received) == 1
    assert len(fill_received) == 0


@pytest.mark.asyncio
async def test_duplicate_subscribe_raises() -> None:
    bus = DomainEventBus()
    async def h(event: ReservationClaimed) -> None: ...
    bus.subscribe(ReservationClaimed, h)
    with pytest.raises(ValueError, match="already subscribed"):
        bus.subscribe(ReservationClaimed, h)


@pytest.mark.asyncio
async def test_release_event_routes_separately() -> None:
    bus = DomainEventBus()
    received: list[ReservationReleased] = []
    async def h(e: ReservationReleased) -> None:
        received.append(e)
    bus.subscribe(ReservationReleased, h)
    rel = ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=True,
    )
    await bus.publish(rel)
    # Bus attaches event_seq and recorded_at_ms; verify core fields match.
    assert len(received) == 1
    assert received[0].cid == rel.cid
    assert received[0].venue_offer_id == rel.venue_offer_id
    assert received[0].reason == rel.reason
    assert received[0].event_seq == 1  # Bus attaches monotonic seq
    assert received[0].recorded_at_ms is not None  # Bus attaches clock time


async def test_subscription_context_subscribes_on_enter_unsubscribes_on_exit() -> None:
    bus = DomainEventBus()
    seen: list[ReservationClaimed] = []

    async def h(event: ReservationClaimed) -> None:
        seen.append(event)

    async with bus.subscription(ReservationClaimed, h):
        await bus.publish(_make_claim())
    # After context exit, handler must be removed
    await bus.publish(_make_claim())
    assert len(seen) == 1


async def test_subscription_unsubscribes_on_exception_in_body() -> None:
    bus = DomainEventBus()
    seen: list[ReservationClaimed] = []

    async def h(event: ReservationClaimed) -> None:
        seen.append(event)

    with pytest.raises(RuntimeError, match="boom"):
        async with bus.subscription(ReservationClaimed, h):
            await bus.publish(_make_claim())
            raise RuntimeError("boom")
    # Even on exception, unsubscribe ran
    await bus.publish(_make_claim())
    assert len(seen) == 1


async def test_subscription_allows_resubscribe_after_exit() -> None:
    bus = DomainEventBus()

    async def h(event: ReservationClaimed) -> None: ...

    async with bus.subscription(ReservationClaimed, h):
        pass
    # Should not raise — handler already removed
    async with bus.subscription(ReservationClaimed, h):
        pass


async def test_subscription_nested_with_existing_subscribe_coexists() -> None:
    bus = DomainEventBus()
    permanent: list[ReservationClaimed] = []
    scoped: list[ReservationClaimed] = []

    async def p_handler(e: ReservationClaimed) -> None:
        permanent.append(e)

    async def s_handler(e: ReservationClaimed) -> None:
        scoped.append(e)

    bus.subscribe(ReservationClaimed, p_handler)
    async with bus.subscription(ReservationClaimed, s_handler):
        await bus.publish(_make_claim())
    await bus.publish(_make_claim())
    assert len(permanent) == 2
    assert len(scoped) == 1
