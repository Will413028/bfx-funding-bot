"""Shared nonce source and request gate for Bitfinex auth (REST + executor + WS).

Bitfinex enforces ONE strictly-increasing nonce PER API KEY, shared across REST
*and* WS auth (docs.bitfinex.com/docs/requirements-and-limitations: "multiple
HTTP OR WebSocket connections ... need separate API keys ... to avoid requests
being rejected on account of an incorrect nonce"). All auth clients in this
daemon share ONE key, so they MUST draw nonces from ONE monotonic source —
otherwise a smaller nonce (the auth WS's ms-scale default vs the REST client's
µs-scale) is rejected as "nonce: small" and that connection can never auth
(this is what left the auth WS flapping forever, fill path silently on REST
reconcile only).

Scale is standardized UP to microseconds: the key has already emitted µs-scale
nonces, so a ms-scale value would be permanently rejected.

A monotonic *generator* is not enough, though: the venue checks the order in
which requests ARRIVE. A caller that takes nonce N and then awaits HTTP can be
overtaken by a concurrent caller holding N+1; when N lands second it is
rejected (Bitfinex answers HTTP 500 with a nonce error body). So the shared
object is an :class:`AuthRequestGate` that owns both the counter and a lock, and
every signed request holds the gate from taking its nonce until its response
arrives (or it fails / times out). Arrival order then equals nonce order.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


def make_monotonic_us_nonce() -> Callable[[], int]:
    """One strictly-increasing µs-scale nonce source. Two calls in the same
    microsecond still return distinct increasing values (the `last + 1` term).

    Generation needs no lock (one asyncio thread, no `await` between reading and
    writing `last`), but generation order is not arrival order: share it only
    through :class:`AuthRequestGate`, never as a bare callable across clients.
    """
    last = 0

    def nonce() -> int:
        nonlocal last
        last = max(last + 1, int(time.time() * 1_000_000))
        return last

    return nonce


class AuthRequestGate:
    """The one nonce counter + lock for every signed call on one API key.

    Hold :meth:`nonce` across "take nonce -> sign -> send -> receive response";
    exiting the block (normally, by exception, timeout or cancellation) lets the
    next caller in. Lock per request, never per paging loop, so a long history
    read cannot starve a trading-path write for more than one round trip.

    One gate per key per process: it cannot order requests made by another
    process on the same key.
    """

    def __init__(self, nonce_source: Callable[[], int] | None = None) -> None:
        self._nonce_source = nonce_source or make_monotonic_us_nonce()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def nonce(self) -> AsyncIterator[int]:
        async with self._lock:
            yield self._nonce_source()

    @property
    def busy(self) -> bool:
        return self._lock.locked()
