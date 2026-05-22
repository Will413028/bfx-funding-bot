from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import OrderFilled


def _make_filled(**overrides) -> OrderFilled:
    base = {
        "cid": 42,
        "venue_offer_id": "v1",
        "credit_id": None,
        "size_usdt": Decimal("100"),
        "fill_rate": 0.0005,
        "signal_correlation_id": uuid4(),
        "account_id": "default",
        "is_simulated": False,
    }
    base.update(overrides)
    return OrderFilled(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_publish_attaches_monotonic_event_seq() -> None:
    captured: list[OrderFilled] = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)

    bus = DomainEventBus(clock=lambda: 9999)
    bus.subscribe(OrderFilled, capture)

    await bus.publish(_make_filled())
    await bus.publish(_make_filled())
    await bus.publish(_make_filled())

    assert [e.event_seq for e in captured] == [1, 2, 3]


@pytest.mark.asyncio
async def test_publish_attaches_recorded_at_ms() -> None:
    captured: list[OrderFilled] = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)

    bus = DomainEventBus(clock=lambda: 12345)
    bus.subscribe(OrderFilled, capture)

    await bus.publish(_make_filled())
    assert captured[0].recorded_at_ms == 12345


@pytest.mark.asyncio
async def test_publish_preserves_caller_set_event_seq() -> None:
    """If caller pre-sets event_seq (e.g. replay path), bus does not overwrite."""
    captured: list[OrderFilled] = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)

    bus = DomainEventBus(clock=lambda: 9999)
    bus.subscribe(OrderFilled, capture)

    await bus.publish(_make_filled(event_seq=42))
    assert captured[0].event_seq == 42


@pytest.mark.asyncio
async def test_publish_preserves_caller_set_recorded_at_ms() -> None:
    captured: list[OrderFilled] = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)

    bus = DomainEventBus(clock=lambda: 9999)
    bus.subscribe(OrderFilled, capture)

    await bus.publish(_make_filled(recorded_at_ms=1000))
    assert captured[0].recorded_at_ms == 1000
