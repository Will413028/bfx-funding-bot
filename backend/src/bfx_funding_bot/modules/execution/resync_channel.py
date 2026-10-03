"""The one place an off-interval reconcile is requested and consumed.

Created first and given to both the reconcile loop (which waits on it) and every producer
of resync hints (the auth WS, the ledger venue hint sink), so a producer never needs the
loop to exist. ``request`` is synchronous and safe from a WS callback on the same loop;
several requests before the loop wakes collapse into one.
"""
from __future__ import annotations

import asyncio


class ResyncChannel:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = ""

    def request(self, reason: str) -> None:
        self._reason = reason  # best-effort: if several callers race, last wins
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def take(self) -> str:
        """The latest reason; clears the request."""
        reason = self._reason
        self._event.clear()
        return reason
