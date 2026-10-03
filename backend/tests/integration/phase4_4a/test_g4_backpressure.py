"""G4: dispatcher bounded queue → put blocks → queue depth stays ≤ queue_max."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import BfxWSEvent, FcnEvent
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.legacy_venue_hints import LegacyVenueHintSink
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher


class _FloodingWSClient:
    """Emits N events as fast as possible — to overflow queue."""
    def __init__(self, count: int) -> None:
        self._count = count
        self._produced = 0
        self._stop = False
        self._max_observed_queue: int = 0

    async def events(self, queue_ref: asyncio.Queue[Any] | None = None) -> AsyncIterator[BfxWSEvent]:
        for i in range(self._count):
            if self._stop:
                return
            yield FcnEvent(
                credit_id=i, symbol="fUSD", side=1,
                mts_create=2000+i, mts_update=2000+i,
                amount=Decimal("100"), rate=0.0005, period_days=2,
                raw_seq=i, raw=[],
            )
            self._produced += 1

    async def close(self) -> None:
        self._stop = True


class _SlowEventSink:
    """Slow event sink to keep the consumer busy so queue depth can build up."""
    async def emit(self, event: dict[str, Any]) -> None:
        await asyncio.sleep(0.005)


class _ObservingDispatcher(BitfinexLiveWSDispatcher):
    """Subclass that tracks the maximum queue depth seen during produce."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_observed_depth: int = 0

    async def _produce(self, stop_event: asyncio.Event) -> None:
        async for ev in self._ws_client.events():
            if stop_event.is_set():
                return
            depth = self._queue.qsize()
            if depth > self.max_observed_depth:
                self.max_observed_depth = depth
            await self._queue.put(ev)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatcher_queue_full_pauses_producer(
    domain_chain: dict[str, Any], event_sink_stub: Any,
) -> None:
    bus = domain_chain["bus"]
    registry = domain_chain["registry"]

    queue_max = 5
    fake_ws = _FloodingWSClient(count=100)
    dispatcher = _ObservingDispatcher(
        ws_client=fake_ws, event_sink=_SlowEventSink(), clock=lambda: 2000, queue_max=queue_max,
        venue_hint_sink=LegacyVenueHintSink(
            registry=registry, bus=bus, persister=NoopEventPersister(), account_id=None),
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))

    # Allow enough time for the producer to flood and get blocked
    await asyncio.sleep(0.3)

    stop.set()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    await fake_ws.close()

    # The bounded queue must never exceed queue_max
    assert dispatcher.max_observed_depth <= queue_max

    # The queue high-water mark must have reached queue_max — confirming backpressure engaged
    assert dispatcher.max_observed_depth == queue_max

    # Producer completed all events (backpressure slowed it, but didn't drop events)
    assert fake_ws._produced == 100
