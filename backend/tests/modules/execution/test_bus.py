"""DomainEventBus — in-process async pub/sub with handler isolation."""
from __future__ import annotations

from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import CancelAcknowledged, CancelRequested


def _make_request() -> CancelRequested:
    return CancelRequested(
        venue_offer_id="x", requested_at_ms=1000, signal_correlation_id=uuid4(),
        account_id="default",
    )


def _make_ack() -> CancelAcknowledged:
    return CancelAcknowledged(
        venue_offer_id="x", acknowledged_at_ms=1001, signal_correlation_id=uuid4(),
        account_id="default", rest_status="success",
    )


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_safe() -> None:
    bus = DomainEventBus()
    await bus.publish(_make_request())  # no raise


@pytest.mark.asyncio
async def test_single_subscriber_receives_event() -> None:
    bus = DomainEventBus()
    received: list[CancelRequested] = []
    async def handler(event: CancelRequested) -> None:
        received.append(event)
    bus.subscribe(CancelRequested, handler)
    request = _make_request()
    await bus.publish(request)
    # The bus forwards the published object itself, unchanged.
    assert len(received) == 1
    assert received[0] is request


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive() -> None:
    bus = DomainEventBus()
    counts = [0, 0, 0]
    async def make_handler(idx: int):
        async def h(event: CancelRequested) -> None:
            counts[idx] += 1
        return h
    bus.subscribe(CancelRequested, await make_handler(0))
    bus.subscribe(CancelRequested, await make_handler(1))
    bus.subscribe(CancelRequested, await make_handler(2))
    await bus.publish(_make_request())
    assert counts == [1, 1, 1]


@pytest.mark.asyncio
async def test_handler_exception_does_not_affect_other_handlers() -> None:
    bus = DomainEventBus()
    survivor_called = False
    async def raiser(event: CancelRequested) -> None:
        raise RuntimeError("boom")
    async def survivor(event: CancelRequested) -> None:
        nonlocal survivor_called
        survivor_called = True
    bus.subscribe(CancelRequested, raiser)
    bus.subscribe(CancelRequested, survivor)
    await bus.publish(_make_request())  # no raise — gather isolates
    assert survivor_called


@pytest.mark.asyncio
async def test_event_type_isolation() -> None:
    bus = DomainEventBus()
    request_received: list[CancelRequested] = []
    ack_received: list[CancelAcknowledged] = []
    async def request_handler(e: CancelRequested) -> None:
        request_received.append(e)
    async def ack_handler(e: CancelAcknowledged) -> None:
        ack_received.append(e)
    bus.subscribe(CancelRequested, request_handler)
    bus.subscribe(CancelAcknowledged, ack_handler)
    await bus.publish(_make_request())
    assert len(request_received) == 1
    assert len(ack_received) == 0


@pytest.mark.asyncio
async def test_duplicate_subscribe_raises() -> None:
    bus = DomainEventBus()
    async def h(event: CancelRequested) -> None: ...
    bus.subscribe(CancelRequested, h)
    with pytest.raises(ValueError, match="already subscribed"):
        bus.subscribe(CancelRequested, h)


@pytest.mark.asyncio
async def test_second_event_type_routes_separately() -> None:
    bus = DomainEventBus()
    received: list[CancelAcknowledged] = []
    async def h(e: CancelAcknowledged) -> None:
        received.append(e)
    bus.subscribe(CancelAcknowledged, h)
    ack = _make_ack()
    await bus.publish(_make_request())
    await bus.publish(ack)
    assert len(received) == 1
    assert received[0] is ack


async def test_subscription_context_subscribes_on_enter_unsubscribes_on_exit() -> None:
    bus = DomainEventBus()
    seen: list[CancelRequested] = []

    async def h(event: CancelRequested) -> None:
        seen.append(event)

    async with bus.subscription(CancelRequested, h):
        await bus.publish(_make_request())
    # After context exit, handler must be removed
    await bus.publish(_make_request())
    assert len(seen) == 1


async def test_subscription_unsubscribes_on_exception_in_body() -> None:
    bus = DomainEventBus()
    seen: list[CancelRequested] = []

    async def h(event: CancelRequested) -> None:
        seen.append(event)

    with pytest.raises(RuntimeError, match="boom"):
        async with bus.subscription(CancelRequested, h):
            await bus.publish(_make_request())
            raise RuntimeError("boom")
    # Even on exception, unsubscribe ran
    await bus.publish(_make_request())
    assert len(seen) == 1


async def test_subscription_allows_resubscribe_after_exit() -> None:
    bus = DomainEventBus()

    async def h(event: CancelRequested) -> None: ...

    async with bus.subscription(CancelRequested, h):
        pass
    # Should not raise — handler already removed
    async with bus.subscription(CancelRequested, h):
        pass


async def test_subscription_nested_with_existing_subscribe_coexists() -> None:
    bus = DomainEventBus()
    permanent: list[CancelRequested] = []
    scoped: list[CancelRequested] = []

    async def p_handler(e: CancelRequested) -> None:
        permanent.append(e)

    async def s_handler(e: CancelRequested) -> None:
        scoped.append(e)

    bus.subscribe(CancelRequested, p_handler)
    async with bus.subscription(CancelRequested, s_handler):
        await bus.publish(_make_request())
    await bus.publish(_make_request())
    assert len(permanent) == 2
    assert len(scoped) == 1
