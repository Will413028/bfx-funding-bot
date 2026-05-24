"""DomainEventBus — in-process async pub/sub for execution domain events.

FORWARD-COMPAT (Phase 5+): outbox pattern upgrade point.
Future: persist event to Postgres outbox table before publish; publisher
service polls outbox and forwards to subscribers with idempotency tokens.
Failed publishes go to DLQ; reconciler retries. Current scope: in-process
gather() with handler isolation; Axiom client's internal buffer+retry
covers transient downstream failures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

log = logging.getLogger(__name__)

EventHandler = Callable[[Any], Awaitable[None]]


class DomainEventBus:
    def __init__(
        self,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._handlers: dict[type, list[EventHandler]] = {}
        self._seq: int = 0
        self._clock = clock if clock is not None else lambda: int(time.time() * 1000)

    def subscribe(self, event_type: type, handler: EventHandler) -> None:
        """Register handler for given event type.

        Raises ValueError if same handler instance subscribed twice — duplicate
        subscriptions are almost certainly a wiring bug.
        """
        existing = self._handlers.setdefault(event_type, [])
        if handler in existing:
            raise ValueError(
                f"handler {handler!r} already subscribed to {event_type.__name__}",
            )
        existing.append(handler)

    async def publish(self, event: Any) -> None:
        """Fan out event to all subscribers of its concrete type.

        If event has event_seq and recorded_at_ms attributes, attaches monotonic
        event_seq (1-indexed) and recorded_at_ms from clock, preserving caller-set
        values (for replay paths).

        Handlers run concurrently via asyncio.gather(return_exceptions=True).
        A failing handler is logged but does not affect siblings or raise to
        caller. Caller treats publish() as best-effort fire; reliability is
        handled by Axiom client buffer+retry (sink layer) and replay (ledger).
        """
        # Attach event_seq and recorded_at_ms if event supports them.
        if hasattr(event, "event_seq") and hasattr(event, "recorded_at_ms"):
            self._seq += 1
            new_event_seq = event.event_seq if event.event_seq is not None else self._seq
            new_recorded_at_ms = (
                event.recorded_at_ms
                if event.recorded_at_ms is not None
                else self._clock()
            )
            event = replace(
                event,
                event_seq=new_event_seq,
                recorded_at_ms=new_recorded_at_ms,
            )

        handlers = self._handlers.get(type(event), [])
        if not handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "bus_handler_failed event=%s handler=%s err=%r",
                    type(event).__name__, getattr(handler, "__qualname__", handler),
                    result,
                )

    @asynccontextmanager
    async def subscription(
        self, event_type: type, handler: EventHandler,
    ) -> AsyncIterator[None]:
        """Scoped subscription — auto-unsubscribe on exit (incl. exception path).

        Use for ephemeral subscribers (smoke recorder, test spies). Permanent
        wirings (ledger) keep using subscribe().
        """
        self.subscribe(event_type, handler)
        try:
            yield
        finally:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)
