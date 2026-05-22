"""Path D — fill_tracker: 5 ticks with state transitions, assert diff events."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import ReservationReleased
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    Phase,
    StrategyName,
)

pytestmark = pytest.mark.integration


class _CaptureAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


def _offer(venue_id: int, cid: int) -> list[Any]:
    return [
        venue_id, "fUSD", 1700000000000, 1700000000000,
        100.0, 100.0, "LIMIT",
        None, None, None, "ACTIVE",
        None, None, None,
        0.0001, 2, 0, 0, None, 0, cid,
    ]


@pytest.mark.asyncio
async def test_fill_tracker_ticks_emit_status_changes() -> None:
    """Tick 1: offers=[venue 111 cid 1].
    Tick 2+: offers=[] — venue 111 disappeared → ReservationReleased via bus.
    """
    tick_seen = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal tick_seen
        if "offers" in req.url.path:
            tick_seen += 1
            if tick_seen == 1:
                return httpx.Response(200, json=[_offer(venue_id=111, cid=1)])
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    axiom = _CaptureAxiom()
    bus = DomainEventBus()
    released: list[ReservationReleased] = []

    async def capture(e: ReservationReleased) -> None:
        released.append(e)

    bus.subscribe(ReservationReleased, capture)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.bitfinex.com",
    ) as client:
        probe = HealthProbe()
        tracker = RestPollingFillTracker(
            http=client,
            axiom=axiom,
            probe=probe,
            bus=bus,
            phase=Phase.PAPER,
            strategy=StrategyName.MEAN_REVERSION,
            cell="fUSD_a30",
            account_id="default",
            poll_interval_s=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(tracker.poll_loop(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    assert len(released) >= 1
    reasons = [e.reason for e in released]
    assert "missing_from_venue" in reasons
