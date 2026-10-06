"""BitfinexLiveWSDispatcher — Bitfinex WS event → VenueHintSink (Phase 4.4a).

Queue/transport shell. Each closing offer (foc) or credit (fcc) becomes an untrusted
venue hint for the injected ``VenueHintSink`` (the ledger's: it requests a reconcile and
publishes a notification; it never writes capital state):

  FocEvent (any status) → offer_closed hint, carrying a cancel this process
                          requested within RECENT_CANCELS_TTL_MS
  FccEvent              → credit_closed hint
  FcnEvent / fcu / control frames → informational no-op
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from bfx_funding_bot.external.bitfinex.auth_ws import (
    BfxWSEvent,
    FccEvent,
    FocEvent,
)
from bfx_funding_bot.modules.ledger import CreditCloseHint, OfferCloseHint, VenueHintSink

log = logging.getLogger(__name__)


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _WSClientProtocol(Protocol):
    def events(self) -> AsyncIterator[BfxWSEvent]: ...


class BitfinexLiveWSDispatcher:
    """Consume BitfinexAuthWSClient stream → scoped VenueHintSink.

    The sink is the ledger's: composition picks it, this class only hands it hints.

    Per spec §6.2 / §4 (G4): bounded queue + queue-depth observability.
    """

    RECENT_CANCELS_TTL_MS = 60_000

    def __init__(
        self,
        *,
        ws_client: _WSClientProtocol,
        event_sink: _EventSink,
        venue_hint_sink: VenueHintSink,
        clock: Callable[[], int] | None = None,
        queue_max: int = 10_000,
    ) -> None:
        self._ws_client = ws_client
        self._events = event_sink
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._queue: asyncio.Queue[BfxWSEvent] = asyncio.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._recent_cancels: dict[str, int] = {}
        self._last_depth_emit_ms: int = 0
        self._venue_hints = venue_hint_sink

    @property
    def queue_depth(self) -> int:
        """Read-only observability accessor (Prometheus saturation gauge
        bfx_ws_dispatcher_queue_depth reads this at scrape time). No behavior."""
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        """Read-only observability accessor — the bounded queue's maxsize."""
        return self._queue_max

    async def handle_cancel_requested(self, event: Any) -> None:
        """Bus subscriber for CancelRequested — tracks recent cancels for 60s."""
        self._recent_cancels[event.venue_offer_id] = event.requested_at_ms

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: drain WS events; periodic maintenance on idle ticks."""
        producer = asyncio.create_task(self._produce(stop_event))
        try:
            while not stop_event.is_set():
                try:
                    bfx_event = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except TimeoutError:
                    self._tick_maintenance()
                    continue
                await self._process(bfx_event)
                self._tick_maintenance()
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer

    async def _produce(self, stop_event: asyncio.Event) -> None:
        async for ev in self._ws_client.events():
            if stop_event.is_set():
                return
            if self._queue.qsize() >= int(self._queue_max * 0.8):
                log.warning("ws_dispatcher_queue_high depth=%d max=%d",
                            self._queue.qsize(), self._queue_max)
            await self._queue.put(ev)

    async def _process(self, bfx_event: BfxWSEvent) -> None:
        now_ms = self._clock()
        if isinstance(bfx_event, FocEvent):
            await self._venue_hints.offer_closed(OfferCloseHint(
                venue_offer_id=bfx_event.venue_offer_id, symbol=bfx_event.symbol,
                kind=bfx_event.status, rate=bfx_event.rate,
                occurred_at_ms=bfx_event.mts_update, venue_seq=bfx_event.raw_seq,
                received_at_ms=now_ms,
                cancel_requested_at_ms=self._recent_cancels.get(bfx_event.venue_offer_id),
            ))
        elif isinstance(bfx_event, FccEvent):
            await self._venue_hints.credit_closed(CreditCloseHint(
                credit_id=bfx_event.credit_id, symbol=bfx_event.symbol,
                amount=bfx_event.amount, rate=bfx_event.rate, period_days=bfx_event.period_days,
                mts_create=bfx_event.mts_create, venue_seq=bfx_event.raw_seq,
                occurred_at_ms=(bfx_event.mts_last_payout
                                if bfx_event.mts_last_payout is not None else bfx_event.mts_update),
                mts_opening=bfx_event.mts_opening, mts_last_payout=bfx_event.mts_last_payout,
            ))
        # fcn/fcu and control frames remain informational no-ops.

    def _tick_maintenance(self) -> None:
        now_ms = self._clock()
        # Cleanup expired recent_cancels
        cancel_cutoff = now_ms - self.RECENT_CANCELS_TTL_MS
        self._recent_cancels = {
            voi: ts for voi, ts in self._recent_cancels.items() if ts >= cancel_cutoff
        }

        # Emit queue depth health every 30s
        if now_ms - self._last_depth_emit_ms > 30_000:
            self._last_depth_emit_ms = now_ms
            depth = self._queue.qsize()
            log.info("ws_dispatcher_queue_depth depth=%d max=%d", depth, self._queue_max)
