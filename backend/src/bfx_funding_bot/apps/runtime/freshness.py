"""Dependency freshness: when a feed's frames count as fresh, and the account's ``ws_data``.

``ws_data`` (the market view that prices a new offer) comes from the account's own funding
book feed, not from market data's candle WS (ADR 2026-10-19 D1, R9c): the book is what the
offer is priced from, and the account must not depend on the recording half's health.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from bfx_funding_bot.core.health import MARKET_DATA_FRESHNESS, HealthProbe
from bfx_funding_bot.modules.execution.readiness import TradingReadiness

# A feed is fresh while its newest frame is younger than this. Bitfinex sends an `hb`
# about every 15s on a quiet channel, so a live feed never comes near it.
FRESH_FRAME_MAX_AGE_MS = 60_000
# How often a feed's frame age is polled.
FRESHNESS_POLL_S = 15.0


class FrameAge(Protocol):
    def last_frame_age_ms(self) -> int | None: ...


def frame_is_fresh(frame_age_ms: int | None) -> bool:
    """A frame arrived on the current connection, and recently (None: none yet)."""
    return frame_age_ms is not None and frame_age_ms < FRESH_FRAME_MAX_AGE_MS


def dependency_answered(probe: HealthProbe, readiness: TradingReadiness | None,
                        dependency: str) -> None:
    """A dependency answered: beat its freshness and clear it in /readyz now,
    not at the next staleness scan (which only ever marks it stale)."""
    probe.record_heartbeat(dependency)
    if readiness is not None:
        readiness.clear_dependency(dependency)


def book_freshness_tick(probe: HealthProbe, readiness: TradingReadiness | None,
                        book: FrameAge) -> None:
    """One poll of the book feed: a fresh frame beats ``ws_data`` and clears it in /readyz.

    A stale feed only stops beating; the staleness scan marks ``ws_data`` stale, /readyz
    answers 503 and HeartbeatGuard blocks new offers. Cancels are not gated by it.
    """
    if frame_is_fresh(book.last_frame_age_ms()):
        dependency_answered(probe, readiness, MARKET_DATA_FRESHNESS)


async def book_freshness_loop(probe: HealthProbe, readiness: TradingReadiness | None,
                              book: FrameAge, stop: asyncio.Event) -> None:
    """Poll the book feed until ``stop``; runs from before the boot (so ``ws_data`` can
    clear while the boot waits) until the account ends."""
    while True:
        book_freshness_tick(probe, readiness, book)
        try:
            await asyncio.wait_for(stop.wait(), timeout=FRESHNESS_POLL_S)
            return
        except TimeoutError:
            pass
